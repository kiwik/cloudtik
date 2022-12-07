# Common Imports
import getopt
import sys


# Parse and get parameters
try:
    opts, args = getopt.getopt(sys.argv[1:], "t:")
except getopt.GetoptError:
    print("Invalid options. Support -t for trials.")
    sys.exit(1)

param_trials = None
for opt, arg in opts:
    if opt in ['-t']:
        param_trials = arg


from cloudtik.runtime.spark.api import ThisSparkCluster
from cloudtik.runtime.ml.api import ThisMLCluster

cluster = ThisSparkCluster()

# Scale the cluster as need
cluster.scale(workers=1)

# Wait for all cluster workers to be ready
cluster.wait_for_ready(min_workers=1)

ml_cluster = ThisMLCluster()
mlflow_url = ml_cluster.get_services()["mlflow"]["url"]


# Initialize SparkSession
from pyspark import SparkConf
from pyspark.sql import SparkSession

spark_conf = SparkConf().setAppName('spark-scikit').set('spark.sql.shuffle.partitions', '16')
spark = SparkSession.builder.config(conf=spark_conf).getOrCreate()
conf = spark.conf


# Load the iris dataset from scikit-learn
from sklearn.datasets import load_iris

iris = iris = load_iris()
X = iris.data
y = iris.target


# Define a train function and objective to minimize.
from sklearn.model_selection import cross_val_score
from sklearn.svm import SVC


# Function to train a model
def train(C):
    # Create a support vector classifier model
    model = SVC(C=C)
    model.fit(X, y)
    return model


# Objective function to minimize
def hyper_objective(C):
    # Create a support vector classifier model
    model = train(C)

    # Use the cross-validation accuracy to compare the models' performance
    accuracy = cross_val_score(model, X, y).mean()
    with mlflow.start_run():
        mlflow.log_metric("C", C)
        mlflow.log_metric("loss", -accuracy)

    # Hyperopt tries to minimize the objective function.
    # A higher accuracy value means a better model, so you must return the negative accuracy.
    return {'loss': -accuracy, 'C': C, 'status': STATUS_OK}


# Do a super parameter tuning with hyperopt
from hyperopt import fmin, tpe, hp, SparkTrials, STATUS_OK, Trials
import mlflow

trials = int(param_trials) if param_trials else 2
print("Hyper parameter tuning trials: {}".format(trials))

# Define the search space and select a search algorithm
search_space = hp.lognormal('C', 0, 1.0)
algo = tpe.suggest
spark_trials = SparkTrials(spark_session=spark)

mlflow.set_tracking_uri(mlflow_url)
mlflow.set_experiment("MLflow + HyperOpt + Scikit-Learn")
argmin = fmin(
  fn=hyper_objective,
  space=search_space,
  algo=algo,
  max_evals=trials,
  trials=spark_trials)

# Print the best value found for C
print("Best parameter found: ", argmin)
print("argmin.get('C'): ", argmin.get('C'))


# Train final model with the best parameters
best_model = train(argmin.get('C'))
model_name = 'scikit-learn-svc-model'
mlflow.sklearn.log_model(best_model, model_name, registered_model_name=model_name)


# Load model as a PyFuncModel and predict on a Pandas DataFrame.
import pandas as pd

model_uri = 'models:/{}/latest'.format(model_name)
print('Inference with model: {}'.format(model_uri))
saved_model = mlflow.pyfunc.load_model(model_uri)
saved_model.predict(pd.DataFrame(X))


# Clean up
spark.stop()
