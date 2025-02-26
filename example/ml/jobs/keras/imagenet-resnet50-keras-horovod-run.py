#
# ResNet-50 model training using Keras and Horovod.
#
# This model is an example of a computation-intensive model that achieves good accuracy on an image
# classification task.  It brings together distributed training concepts such as learning rate
# schedule adjustments with a warmup, randomized data reading, and checkpointing on the first worker
# only.
#
# Note: This model uses Keras native ImageDataGenerator and not the sophisticated preprocessing
# pipeline that is typically used to train state-of-the-art ResNet-50 model.  This results in ~0.5%
# increase in the top-1 validation error compared to the single-crop top-1 validation error from
# https://github.com/KaimingHe/deep-residual-networks.
#
import argparse
import os


parser = argparse.ArgumentParser(description='Keras ImageNet Example',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--num-proc', type=int,
                    help='number of worker processes for training')

parser.add_argument('--train-dir', default=os.path.expanduser('~/imagenet/train'),
                    help='path to training data')
parser.add_argument('--val-dir', default=os.path.expanduser('~/imagenet/validation'),
                    help='path to validation data')
parser.add_argument('--log-dir', default='./logs',
                    help='tensorboard log directory')
parser.add_argument('--checkpoint-format', default='./resnet50-checkpoint-{epoch}.h5',
                    help='checkpoint file format')
parser.add_argument('--fp16-allreduce', action='store_true', default=False,
                    help='use fp16 compression during allreduce')

# Default settings from https://arxiv.org/abs/1706.02677.
parser.add_argument('--batch-size', type=int, default=32,
                    help='input batch size for training')
parser.add_argument('--val-batch-size', type=int, default=32,
                    help='input batch size for validation')
parser.add_argument('--epochs', type=int, default=90,
                    help='number of epochs to train')
parser.add_argument('--base-lr', type=float, default=0.0125,
                    help='learning rate for a single GPU')
parser.add_argument('--warmup-epochs', type=float, default=5,
                    help='number of warmup epochs')
parser.add_argument('--momentum', type=float, default=0.9,
                    help='SGD momentum')
parser.add_argument('--wd', type=float, default=0.00005,
                    help='weight decay')

parser.add_argument('--gloo', action='store_true', dest='use_gloo',
                    help='Run Horovod using the Gloo controller. This will '
                         'be the default if Horovod was not built with MPI support.')
parser.add_argument('--mpi', action='store_true', dest='use_mpi',
                    help='Run Horovod using the MPI controller. This will '
                         'be the default if Horovod was built with MPI support.')

# Serialize and deserialize keras model
import io
import h5py


def serialize_keras_model(model):
    """Serialize model into byte array encoded into base 64."""
    bio = io.BytesIO()
    with h5py.File(bio, 'w') as f:
        keras.models.save_model(model, f)
    return bio.getvalue()


def deserialize_keras_model(model_bytes, custom_objects):
    """Deserialize model from byte array encoded in base 64."""
    bio = io.BytesIO(model_bytes)
    with h5py.File(bio, 'r') as f:
        with keras.utils.custom_object_scope(custom_objects):
            return keras.models.load_model(f)


import keras
from keras import backend as K
from keras.preprocessing import image
import tensorflow as tf
import horovod.keras as hvd


def train_horovod(learning_rate):
    # Horovod: initialize Horovod.
    hvd.init()

    # Horovod: pin GPU to be used to process local rank (one GPU per process)
    # config = tf.ConfigProto()
    # config.gpu_options.allow_growth = True
    # config.gpu_options.visible_device_list = str(hvd.local_rank())
    # K.set_session(tf.Session(config=config))

    # If set > 0, will resume training from a given checkpoint.
    resume_from_epoch = 0
    for try_epoch in range(args.epochs, 0, -1):
        if os.path.exists(args.checkpoint_format.format(epoch=try_epoch)):
            resume_from_epoch = try_epoch
            break

    # Horovod: broadcast resume_from_epoch from rank 0 (which will have
    # checkpoints) to other ranks.
    resume_from_epoch = hvd.broadcast(resume_from_epoch, 0, name='resume_from_epoch')

    # Horovod: print logs on the first worker.
    verbose = 1 if hvd.rank() == 0 else 0

    # Training data iterator.
    train_gen = image.ImageDataGenerator(
        width_shift_range=0.33, height_shift_range=0.33, zoom_range=0.5, horizontal_flip=True,
        preprocessing_function=keras.applications.resnet.preprocess_input)
    train_iter = train_gen.flow_from_directory(args.train_dir,
                                               batch_size=args.batch_size,
                                               target_size=(224, 224))

    # Validation data iterator.
    test_gen = image.ImageDataGenerator(
        zoom_range=(0.875, 0.875), preprocessing_function=keras.applications.resnet.preprocess_input)
    test_iter = test_gen.flow_from_directory(args.val_dir,
                                             batch_size=args.val_batch_size,
                                             target_size=(224, 224))

    # Set up standard ResNet-50 model.
    model = keras.applications.resnet.ResNet50(weights=None)

    # Horovod: (optional) compression algorithm.
    compression = hvd.Compression.fp16 if args.fp16_allreduce else hvd.Compression.none

    # Horovod: adjust learning rate based on number of GPUs.
    initial_lr = learning_rate * hvd.size()

    # Restore from a previous checkpoint, if initial_epoch is specified.
    # Horovod: restore on the first worker which will broadcast both model and optimizer weights
    # to other workers.
    if resume_from_epoch > 0 and hvd.rank() == 0:
        model = hvd.load_model(args.checkpoint_format.format(epoch=resume_from_epoch),
                               compression=compression)
    else:
        # ResNet-50 model that is included with Keras is optimized for inference.
        # Add L2 weight decay & adjust BN settings.
        model_config = model.get_config()
        for layer, layer_config in zip(model.layers, model_config['layers']):
            if hasattr(layer, 'kernel_regularizer'):
                regularizer = keras.regularizers.l2(args.wd)
                layer_config['config']['kernel_regularizer'] = \
                    {'class_name': regularizer.__class__.__name__,
                     'config': regularizer.get_config()}
            if type(layer) == keras.layers.BatchNormalization:
                layer_config['config']['momentum'] = 0.9
                layer_config['config']['epsilon'] = 1e-5

        model = keras.models.Model.from_config(model_config)
        opt = keras.optimizers.SGD(lr=initial_lr, momentum=args.momentum)

        # Horovod: add Horovod Distributed Optimizer.
        opt = hvd.DistributedOptimizer(opt, compression=compression)

        model.compile(loss=keras.losses.categorical_crossentropy,
                      optimizer=opt,
                      metrics=['accuracy', 'top_k_categorical_accuracy'])

    callbacks = [
        # Horovod: broadcast initial variable states from rank 0 to all other processes.
        # This is necessary to ensure consistent initialization of all workers when
        # training is started with random weights or restored from a checkpoint.
        hvd.callbacks.BroadcastGlobalVariablesCallback(0),

        # Horovod: average metrics among workers at the end of every epoch.
        #
        # Note: This callback must be in the list before the ReduceLROnPlateau,
        # TensorBoard, or other metrics-based callbacks.
        hvd.callbacks.MetricAverageCallback(),

        # Horovod: using `lr = 1.0 * hvd.size()` from the very beginning leads to worse final
        # accuracy. Scale the learning rate `lr = 1.0` ---> `lr = 1.0 * hvd.size()` during
        # the first five epochs. See https://arxiv.org/abs/1706.02677 for details.
        hvd.callbacks.LearningRateWarmupCallback(initial_lr=initial_lr,
                                                 warmup_epochs=args.warmup_epochs,
                                                 verbose=verbose),

        # Horovod: after the warmup reduce learning rate by 10 on the 30th, 60th and 80th epochs.
        hvd.callbacks.LearningRateScheduleCallback(initial_lr=initial_lr,
                                                   multiplier=1.,
                                                   start_epoch=args.warmup_epochs,
                                                   end_epoch=30),
        hvd.callbacks.LearningRateScheduleCallback(initial_lr=initial_lr, multiplier=1e-1, start_epoch=30, end_epoch=60),
        hvd.callbacks.LearningRateScheduleCallback(initial_lr=initial_lr, multiplier=1e-2, start_epoch=60, end_epoch=80),
        hvd.callbacks.LearningRateScheduleCallback(initial_lr=initial_lr, multiplier=1e-3, start_epoch=80),
    ]

    # Horovod: save checkpoints only on the first worker to prevent other workers from corrupting them.
    if hvd.rank() == 0:
        callbacks.append(keras.callbacks.ModelCheckpoint(args.checkpoint_format))
        callbacks.append(keras.callbacks.TensorBoard(args.log_dir))

    # Train the model. The training will randomly sample 1 / N batches of training data and
    # 3 / N batches of validation data on every worker, where N is the number of workers.
    # Over-sampling of validation data helps to increase probability that every validation
    # example will be evaluated.
    model.fit_generator(train_iter,
                        steps_per_epoch=len(train_iter) // hvd.size(),
                        callbacks=callbacks,
                        epochs=args.epochs,
                        verbose=verbose,
                        workers=4,
                        initial_epoch=resume_from_epoch,
                        validation_data=test_iter,
                        validation_steps=3 * len(test_iter) // hvd.size())

    # Evaluate the model on the full data set.
    score = hvd.allreduce(model.evaluate_generator(test_iter, len(test_iter), workers=4))
    if verbose:
        print('Test loss:', score[0])
        print('Test accuracy:', score[1])

    if hvd.rank() == 0:
        # Return the model bytes
        return serialize_keras_model(model)


if __name__ == '__main__':
    args = parser.parse_args()

    # CloudTik cluster preparation or information
    from cloudtik.runtime.spark.api import ThisSparkCluster

    cluster = ThisSparkCluster()

    # Scale the cluster as need
    # cluster.scale(workers=1)

    # Wait for all cluster workers to be ready
    cluster.wait_for_ready(min_workers=1)

    # Total worker cores
    cluster_info = cluster.get_info()

    if not args.num_proc:
        total_workers = cluster_info.get("total-workers")
        if total_workers:
            args.num_proc = total_workers
        if not args.num_proc:
            args.num_proc = 1

    worker_ips = cluster.get_worker_node_ips()

    #  Hyperopt training function
    import horovod

    # Set the parameters
    num_proc = args.num_proc
    print("Train processes: {}".format(num_proc))

    # Generate the host list
    worker_num_proc = int(num_proc / len(worker_ips))
    if not worker_num_proc:
        worker_num_proc = 1
    host_slots = ["{}:{}".format(worker_ip, worker_num_proc) for worker_ip in worker_ips]
    hosts = ",".join(host_slots)
    print("Hosts to run:", hosts)

    learning_rate = args.base_lr
    print("Train learning rate: {}".format(learning_rate))

    model_bytes = horovod.run(
        train_horovod, args=(learning_rate,),
        num_proc=num_proc, hosts=hosts,
        use_gloo=args.use_gloo, use_mpi=args.use_mpi,
        verbose=2)[0]
    model = deserialize_keras_model(model_bytes, custom_objects={})
