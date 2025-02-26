import argparse
import errno
import glob
import json
import logging.handlers
import os
import platform
import re
import shutil
import time
import traceback

import cloudtik.core._private.constants as constants
import cloudtik.core._private.services as services
import cloudtik.core._private.utils as utils
from cloudtik.core._private.logging_utils import setup_component_logger

# TODO (haifeng): check what is this comment about
# Logger for this module. It should be configured at the entry point
# into the program using CloudTik. It provides a default configuration at
# entry/init points.
logger = logging.getLogger(__name__)

# The groups are worker id, job id, and pid.
JOB_LOG_PATTERN = re.compile(".*worker-([0-9a-f]+)-([0-9a-f]+)-(\d+)")
# The groups are job id.
RUNTIME_ENV_SETUP_PATTERN = re.compile(".*runtime_env_setup-(\d+).log")
# Log name update interval under pressure.
# We need it because log name update is CPU intensive and uses 100%
# of cpu when there are many log files.
LOG_NAME_UPDATE_INTERVAL_S = float(
    os.getenv("CLOUDTIK_NAME_UPDATE_INTERVAL_S", 0.5))
# Once there are more files than this threshold,
# log monitor start giving backpressure to lower cpu usages.
LOG_MONITOR_MANY_FILES_THRESHOLD = int(
    os.getenv("CLOUDTIK_LOG_MONITOR_MANY_FILES_THRESHOLD", 1000))


class LogFileInfo:
    def __init__(self,
                 filename=None,
                 size_when_last_opened=None,
                 file_position=None,
                 file_handle=None,
                 is_err_file=False,
                 job_id=None,
                 worker_pid=None):
        assert (filename is not None and size_when_last_opened is not None
                and file_position is not None)
        self.filename = filename
        self.size_when_last_opened = size_when_last_opened
        self.file_position = file_position
        self.file_handle = file_handle
        self.is_err_file = is_err_file
        self.job_id = job_id
        self.worker_pid = worker_pid
        self.actor_name = None
        self.task_name = None


class LogMonitor:
    """A monitor process for monitoring log files.

    This class mantains a list of open files and a list of closed log files. We
    can't simply leave all files open because we'll run out of file
    descriptors.

    The "run" method of this class will cycle between doing several things:
    1. First, it will check if any new files have appeared in the log
       directory. If so, they will be added to the list of closed files.
    2. Then, if we are unable to open any new files, we will close all of the
       files.
    3. Then, we will open as many closed files as we can that may have new
       lines (judged by an increase in file size since the last time the file
       was opened).
    4. Then we will loop through the open files and see if there are any new
       lines in the file. If so, we will publish them to Redis.

    Attributes:
        host (str): The hostname of this machine. Used to improve the log
            messages published to Redis.
        logs_dir (str): The directory that the log files are in.
        redis_client: A client used to communicate with the Redis server.
        log_filenames (set): This is the set of filenames of all files in
            open_file_infos and closed_file_infos.
        open_file_infos (list[LogFileInfo]): Info for all of the open files.
        closed_file_infos (list[LogFileInfo]): Info for all of the closed
            files.
        can_open_more_files (bool): True if we can still open more files and
            false otherwise.
    """

    def __init__(self,
                 logs_dir,
                 redis_address,
                 redis_password=None):
        """Initialize the log monitor object."""
        self.ip = services.get_node_ip_address()
        self.logs_dir = logs_dir
        self.redis_client = services.create_redis_client(
            redis_address, password=redis_password)
        self.log_filenames = set()
        self.open_file_infos = []
        self.closed_file_infos = []
        self.can_open_more_files = True

    def close_all_files(self):
        """Close all open files (so that we can open more)."""
        while len(self.open_file_infos) > 0:
            file_info = self.open_file_infos.pop(0)
            file_info.file_handle.close()
            file_info.file_handle = None
            try:
                # Test if the worker process that generated the log file
                # is still alive. Only applies to worker processes.
                if (file_info.worker_pid != "cloudtik_node_controller"
                        and file_info.worker_pid != "cloudtik_cluster_controller"
                        and file_info.worker_pid is not None):
                    assert not isinstance(file_info.worker_pid, str), (
                        "PID should be an int type. "
                        "Given PID: {file_info.worker_pid}.")
                    os.kill(file_info.worker_pid, 0)
            except OSError:
                # The process is not alive any more, so move the log file
                # out of the log directory so glob.glob will not be slowed
                # by it.
                target = os.path.join(self.logs_dir, "old",
                                      os.path.basename(file_info.filename))
                try:
                    shutil.move(file_info.filename, target)
                except (IOError, OSError) as e:
                    if e.errno == errno.ENOENT:
                        logger.warning(
                            f"Warning: The file {file_info.filename} "
                            "was not found.")
                    else:
                        raise e
            else:
                self.closed_file_infos.append(file_info)
        self.can_open_more_files = True

    def update_log_filenames(self):
        """Update the list of log files to monitor."""
        # output of user code is written here
        log_file_paths = glob.glob(f"{self.logs_dir}/worker*[.out|.err]")
        # segfaults and other serious errors are logged here
        node_controller_log_paths = glob.glob(f"{self.logs_dir}/cloudtik_node_controller*[.out|.err]")
        # monitor logs are needed to report cluster scaler events
        cluster_controller_log_paths = glob.glob(f"{self.logs_dir}/cloudtik_cluster_controller*[.out|.err]")
        # runtime_env setup process is logged here
        runtime_env_setup_paths = glob.glob(
            f"{self.logs_dir}/runtime_env*.log")
        total_files = 0
        for file_path in (log_file_paths + node_controller_log_paths +
                          cluster_controller_log_paths + runtime_env_setup_paths):
            if os.path.isfile(
                    file_path) and file_path not in self.log_filenames:
                job_match = JOB_LOG_PATTERN.match(file_path)
                if job_match:
                    job_id = job_match.group(2)
                    worker_pid = int(job_match.group(3))
                else:
                    job_id = None
                    worker_pid = None

                # Perform existence check first because most file will not be
                # including runtime_env. This saves some cpu cycle.
                if "runtime_env" in file_path:
                    runtime_env_job_match = RUNTIME_ENV_SETUP_PATTERN.match(
                        file_path)
                    if runtime_env_job_match:
                        job_id = runtime_env_job_match.group(1)

                is_err_file = file_path.endswith("err")

                self.log_filenames.add(file_path)
                self.closed_file_infos.append(
                    LogFileInfo(
                        filename=file_path,
                        size_when_last_opened=0,
                        file_position=0,
                        file_handle=None,
                        is_err_file=is_err_file,
                        job_id=job_id,
                        worker_pid=worker_pid))
                log_filename = os.path.basename(file_path)
                logger.info(f"Beginning to track file {log_filename}")
            total_files += 1
        return total_files

    def open_closed_files(self):
        """Open some closed files if they may have new lines.

        Opening more files may require us to close some of the already open
        files.
        """
        if not self.can_open_more_files:
            # If we can't open any more files. Close all of the files.
            self.close_all_files()

        files_with_no_updates = []
        while len(self.closed_file_infos) > 0:
            if (len(self.open_file_infos) >=
                    constants.LOG_MONITOR_MAX_OPEN_FILES):
                self.can_open_more_files = False
                break

            file_info = self.closed_file_infos.pop(0)
            assert file_info.file_handle is None
            # Get the file size to see if it has gotten bigger since we last
            # opened it.
            try:
                file_size = os.path.getsize(file_info.filename)
            except (IOError, OSError) as e:
                # Catch "file not found" errors.
                if e.errno == errno.ENOENT:
                    logger.warning(f"Warning: The file {file_info.filename} "
                                   "was not found.")
                    self.log_filenames.remove(file_info.filename)
                    continue
                raise e

            # If some new lines have been added to this file, try to reopen the
            # file.
            if file_size > file_info.size_when_last_opened:
                try:
                    f = open(file_info.filename, "rb")
                except (IOError, OSError) as e:
                    if e.errno == errno.ENOENT:
                        logger.warning(
                            f"Warning: The file {file_info.filename} "
                            "was not found.")
                        self.log_filenames.remove(file_info.filename)
                        continue
                    else:
                        raise e

                f.seek(file_info.file_position)
                file_info.filesize_when_last_opened = file_size
                file_info.file_handle = f
                self.open_file_infos.append(file_info)
            else:
                files_with_no_updates.append(file_info)

        # Add the files with no changes back to the list of closed files.
        self.closed_file_infos += files_with_no_updates

    def check_log_files_and_publish_updates(self):
        """Get any changes to the log files and push updates to Redis.

        Returns:
            True if anything was published and false otherwise.
        """
        anything_published = False
        lines_to_publish = []

        def flush():
            nonlocal lines_to_publish
            nonlocal anything_published
            if len(lines_to_publish) > 0:
                data = {
                    "ip": self.ip,
                    "pid": file_info.worker_pid,
                    "job": file_info.job_id,
                    "is_err": file_info.is_err_file,
                    "lines": lines_to_publish,
                    "actor_name": file_info.actor_name,
                    "task_name": file_info.task_name,
                }
                self.redis_client.publish(constants.LOG_FILE_CHANNEL,
                                          json.dumps(data))
                anything_published = True
                lines_to_publish = []

        for file_info in self.open_file_infos:
            assert not file_info.file_handle.closed

            max_num_lines_to_read = 100
            for _ in range(max_num_lines_to_read):
                try:
                    next_line = file_info.file_handle.readline()
                    # Replace any characters not in UTF-8 with
                    # a replacement character, see
                    # https://stackoverflow.com/a/38565489/10891801
                    next_line = next_line.decode("utf-8", "replace")
                    if next_line == "":
                        break
                    if next_line[-1] == "\n":
                        next_line = next_line[:-1]
                    lines_to_publish.append(next_line)
                except Exception:
                    logger.error(
                        f"Error: Reading file: {file_info.filename}, "
                        f"position: {file_info.file_info.file_handle.tell()} "
                        "failed.")
                    raise

            # TODO (haifeng) : correct and add the processes we will have
            if file_info.file_position == 0:
                if "/cloudtik_node_controller" in file_info.filename:
                    file_info.worker_pid = "cloudtik_node_controller"
                elif "/cloudtik_cluster_controller" in file_info.filename:
                    file_info.worker_pid = "cloudtik_cluster_controller"

            # Record the current position in the file.
            file_info.file_position = file_info.file_handle.tell()
            flush()

        return anything_published

    def run(self):
        """Run the log monitor.

        This will query Redis once every second to check if there are new log
        files to monitor. It will also store those log files in Redis.
        """
        total_log_files = 0
        last_updated = time.time()
        while True:
            elapsed_seconds = int(time.time() - last_updated)
            if (total_log_files < LOG_MONITOR_MANY_FILES_THRESHOLD
                    or elapsed_seconds > LOG_NAME_UPDATE_INTERVAL_S):
                total_log_files = self.update_log_filenames()
                last_updated = time.time()
            self.open_closed_files()
            anything_published = self.check_log_files_and_publish_updates()
            # If nothing was published, then wait a little bit before checking
            # for logs to avoid using too much CPU.
            if not anything_published:
                time.sleep(0.1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Parse Redis server for the "
                     "log monitor to connect "
                     "to."))
    parser.add_argument(
        "--redis-address",
        required=True,
        type=str,
        help="The address to use for Redis.")
    parser.add_argument(
        "--redis-password",
        required=False,
        type=str,
        default=None,
        help="the password to use for Redis")
    parser.add_argument(
        "--logging-level",
        required=False,
        type=str,
        default=constants.LOGGER_LEVEL_INFO,
        choices=constants.LOGGER_LEVEL_CHOICES,
        help=constants.LOGGER_LEVEL_HELP)
    parser.add_argument(
        "--logging-format",
        required=False,
        type=str,
        default=constants.LOGGER_FORMAT,
        help=constants.LOGGER_FORMAT_HELP)
    parser.add_argument(
        "--logging-filename",
        required=False,
        type=str,
        default=constants.LOG_FILE_NAME_LOG_MONITOR,
        help="Specify the name of log file, "
        "log to stdout if set empty, default is "
        f"\"{constants.LOG_FILE_NAME_LOG_MONITOR}\"")
    parser.add_argument(
        "--logs-dir",
        required=True,
        type=str,
        help="Specify the path of the temporary directory used by cluster "
        "processes.")
    parser.add_argument(
        "--logging-rotate-bytes",
        required=False,
        type=int,
        default=constants.LOGGING_ROTATE_MAX_BYTES,
        help="Specify the max bytes for rotating "
        "log file, default is "
        f"{constants.LOGGING_ROTATE_MAX_BYTES} bytes.")
    parser.add_argument(
        "--logging-rotate-backup-count",
        required=False,
        type=int,
        default=constants.LOGGING_ROTATE_BACKUP_COUNT,
        help="Specify the backup count of rotated log file, default is "
        f"{constants.LOGGING_ROTATE_BACKUP_COUNT}.")
    args = parser.parse_args()
    setup_component_logger(
        logging_level=args.logging_level,
        logging_format=args.logging_format,
        log_dir=args.logs_dir,
        filename=args.logging_filename,
        max_bytes=args.logging_rotate_bytes,
        backup_count=args.logging_rotate_backup_count)

    log_monitor = LogMonitor(
        args.logs_dir,
        args.redis_address,
        redis_password=args.redis_password)

    try:
        log_monitor.run()
    except Exception as e:
        # Something went wrong
        redis_client = services.create_redis_client(
            args.redis_address, password=args.redis_password)
        traceback_str = utils.format_error_message(
            traceback.format_exc())
        message = (f"The log monitor on node {platform.node()} "
                   f"failed with the following error:\n{traceback_str}")
        utils.publish_error(
            constants.ERROR_CLUSTER_CONTROLLER_DIED,
            message,
            redis_client=redis_client)
        logger.error(message)
        raise e
