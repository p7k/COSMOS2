import os
import json
import itertools as it
import shutil

from sqlalchemy.orm import relationship, synonym, backref
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy import Column, Boolean, Integer, String, PickleType, ForeignKey, DateTime, func, Table, BigInteger, \
    Text
from flask import url_for
from networkx.algorithms import breadth_first_search

from ..db import Base
from ..util.sqla import Enum34_ColumnType, MutableDict
from .. import TaskStatus, StageStatus, signal_task_status_change
from ..util.helpers import wait_for_file


opj = os.path.join


class ExpectedError(Exception): pass


class ToolError(Exception): pass


class ToolValidationError(Exception): pass


class GetOutputError(Exception): pass


task_failed_printout = """Failure Info:
<COMMAND path={0.output_command_script_path} drm_jobID={0.drm_jobID}>
{0.command_script_text}
</COMMAND>
<STDOUT path={0.output_stdout_path}>
{0.stdout_text}
</STDOUT>
<STDERR path={0.output_stderr_path}>
{0.stderr_text}
</STDERR>
Failed Task.output_dir: {0.output_dir}"""


@signal_task_status_change.connect
def task_status_changed(task):
    if task.status in [TaskStatus.successful]:
        if not task.NOOP:
            task.log.info('%s %s' % (task, task.status))

    if task.status == TaskStatus.waiting:
        task.started_on = func.now()

    elif task.status == TaskStatus.submitted:
        if not task.NOOP:
            task.log.info('%s %s. drm=%s; drm_jobid=%s' % (task, task.status, task.drm, task.drm_jobID))
        task.submitted_on = func.now()
        task.stage.status = StageStatus.running

    elif task.status == TaskStatus.failed:
        if not task.must_succeed:
            task.log.warn('%s failed, but must_succeed is False' % task)
            task.log.warn(task_failed_printout.format(task))
            task.finished_on = func.now()
        else:
            task.log.warn('%s attempt #%s failed (max_attempts=%s)' % (task, task.attempt, task.execution.max_attempts))
            if task.attempt < task.execution.max_attempts:
                task.log.warn(task_failed_printout.format(task))
                task.attempt += 1
                task.status = TaskStatus.no_attempt
            else:
                wait_for_file(task.execution, task.output_stderr_path, 60)

                task.log.warn(task_failed_printout.format(task))
                task.log.error('%s has failed too many times' % task)
                task.finished_on = func.now()
                task.stage.status = StageStatus.failed
                # task.session.commit()

    elif task.status == TaskStatus.successful:
        task.successful = True
        task.finished_on = func.now()
        if all(t.successful or not t.must_succeed for t in task.stage.tasks):
            task.stage.status = StageStatus.successful

            # task.session.commit()


task_edge_table = Table('task_edge', Base.metadata,
                        Column('parent_id', Integer, ForeignKey('task.id'), primary_key=True),
                        Column('child_id', Integer, ForeignKey('task.id'), primary_key=True))


def logplus(filename):
    prefix, suffix = os.path.splitext(filename)
    return property(lambda self: opj(self.log_dir, "{0}_attempt{1}{2}".format(prefix, self.attempt, suffix)))


def readfile(path):
    if not os.path.exists(path):
        return 'file does not exist'
    with open(path, 'r') as fh:
        return fh.read()


class Task(Base):
    __tablename__ = 'task'
    """
    A job that gets executed.  Has a unique set of tags within its Stage.
    """
    # causes a problem with mysql.  its checked a the application level so should be okay
    # __table_args__ = (UniqueConstraint('tags', 'stage_id', name='_uc1'),)

    id = Column(Integer, primary_key=True)
    mem_req = Column(Integer, default=None)
    cpu_req = Column(Integer, default=1)
    time_req = Column(Integer)
    NOOP = Column(Boolean, default=False, nullable=False)
    tags = Column(MutableDict.as_mutable(PickleType), nullable=False)
    # tags = Column(MutableDict.as_mutable(JSONEncodedDict))
    stage_id = Column(ForeignKey('stage.id'), nullable=False)
    stage = relationship("Stage", backref=backref("tasks", cascade="all, delete-orphan"))
    log_dir = Column(String(255))
    output_dir = Column(String(255))
    _status = Column(Enum34_ColumnType(TaskStatus), default=TaskStatus.no_attempt)
    successful = Column(Boolean, default=False, nullable=False)
    started_on = Column(DateTime)
    submitted_on = Column(DateTime)
    finished_on = Column(DateTime)
    attempt = Column(Integer, default=1)
    must_succeed = Column(Boolean, default=True)
    drm = Column(String(255), nullable=False)
    parents = relationship("Task",
                           secondary=task_edge_table,
                           primaryjoin=id == task_edge_table.c.parent_id,
                           secondaryjoin=id == task_edge_table.c.child_id,
                           backref='children')
    #command = Column(Text)

    @property
    def input_files(self):
        return [ifa.taskfile for ifa in self._input_file_assocs]

    drm_native_specification = Column(String(255))
    drm_jobID = Column(Integer)

    profile_fields = ['wall_time', 'cpu_time', 'percent_cpu', 'user_time', 'system_time', 'io_read_count', 'io_write_count', 'io_read_kb', 'io_write_kb',
                      'ctx_switch_voluntary', 'ctx_switch_involuntary', 'avg_rss_mem_kb', 'max_rss_mem_kb', 'avg_vms_mem_kb', 'max_vms_mem_kb', 'avg_num_threads', 'max_num_threads',
                      'avg_num_fds', 'max_num_fds', 'exit_status']
    exclude_from_dict = profile_fields + ['command', 'info']

    exit_status = Column(Integer)

    percent_cpu = Column(Integer)
    wall_time = Column(BigInteger)

    cpu_time = Column(BigInteger)
    user_time = Column(BigInteger)
    system_time = Column(BigInteger)

    avg_rss_mem_kb = Column(BigInteger)
    max_rss_mem_kb = Column(BigInteger)
    avg_vms_mem_kb = Column(BigInteger)
    max_vms_mem_kb = Column(BigInteger)

    io_read_count = Column(BigInteger)
    io_write_count = Column(BigInteger)
    io_read_kb = Column(BigInteger)
    io_write_kb = Column(BigInteger)

    ctx_switch_voluntary = Column(BigInteger)
    ctx_switch_involuntary = Column(BigInteger)

    avg_num_threads = Column(BigInteger)
    max_num_threads = Column(BigInteger)

    avg_num_fds = Column(Integer)
    max_num_fds = Column(Integer)


    @declared_attr
    def status(cls):
        def get_status(self):
            return self._status

        def set_status(self, value):
            if self._status != value:
                self._status = value
                signal_task_status_change.send(self)

        return synonym('_status', descriptor=property(get_status, set_status))


    @property
    def execution(self):
        return self.stage.execution

    @property
    def log(self):
        return self.execution.log

    @property
    def finished(self):
        return self.status in [TaskStatus.successful, TaskStatus.failed]

    _cache_profile = None

    output_profile_path = logplus('profile.json')
    output_command_script_path = logplus('command.bash')
    output_stderr_path = logplus('stderr.txt')
    output_stdout_path = logplus('stdout.txt')

    @property
    def stdout_text(self):
        return readfile(self.output_stdout_path).strip()

    @property
    def stderr_text(self):
        return readfile(self.output_stderr_path).strip()

    @property
    def command_script_text(self):
        return readfile(self.output_command_script_path).strip()

    @property
    def forwarded_inputs(self):
        return [ifa.taskfile for ifa in self._input_file_assocs if ifa.forward]

    # @property
    # def all_outputs(self):
    #     """
    #     :return: all output taskfiles, including any being forwarded
    #     """
    #     return self.output_files + self.forwarded_inputs

    @property
    def profile(self):
        if self.NOOP:
            return {}
        if self._cache_profile is None:
            if wait_for_file(self.execution, self.output_profile_path, 60, error=False):
                with open(self.output_profile_path, 'r') as fh:
                    self._cache_profile = json.load(fh)
            else:
                self.log.warn('%s does not exist on the filesystem' % self.output_profile_path)
                return {}
        return self._cache_profile

    def update_from_profile_output(self):
        for k, v in self.profile.items():
            setattr(self, k, v)

    def successors(self):
        """
        :return: (list) all tasks that descend from this task in the task_graph
        """
        return set(it.chain(*breadth_first_search.bfs_successors(self.ex.task_graph(), self).values()))

    @property
    def label(self):
        """Label used for the taskgraph image"""
        tags = '' if len(self.tags) == 0 else "\\n {0}".format(
            "\\n".join(["{0}: {1}".format(k, v) for k, v in self.tags.items()]))

        return "[%s] %s%s" % (self.id, self.stage.name, tags)

    def tags_as_query_string(self):
        import urllib

        return urllib.urlencode(self.tags)

    def delete(self, delete_files=False):
        self.log.debug('Deleting %s' % self)
        if delete_files:
            for tf in self.output_files:
                tf.delete(True)
            if os.path.exists(self.log_dir):
                shutil.rmtree(self.log_dir)

        self.session.delete(self)
        self.session.commit()

    @property
    def url(self):
        return url_for('cosmos.task', id=self.id)

    def __repr__(self):
        s = self.stage.name if self.stage else ''
        return '<Task[%s] %s %s>' % (self.id or 'id_%s' % id(self), s, self.tags)