
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"


import os
import copy
import time
import errno
import Queue
import tempfile
import threading
import traceback

from orte_cffi import ffi, lib as orte_lib

from .... import pilot     as rp
from ...  import utils     as rpu

from .base import AgentExecutingComponent

# ----------------------------------------------------------------------------------
#
def rec_makedir(target):

    # recursive makedir which ignores errors if dir already exists

    try:
        os.makedirs(target)

    except OSError as e:
        # ignore failure on existing directory
        if e.errno == errno.EEXIST and os.path.isdir(os.path.dirname(target)):
            pass
        else:
            raise


# ==============================================================================
#
@ffi.def_extern()
def launch_cb(task, jdata, status, cbdata):
    return ffi.from_handle(cbdata).unit_spawned_cb(task)


# ==============================================================================
#
@ffi.def_extern()
def finish_cb(task, jdata, status, cbdata):
    return ffi.from_handle(cbdata).unit_completed_cb(task, status)


# ==============================================================================
#
class ORTE(AgentExecutingComponent):

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg):

        AgentExecutingComponent.__init__ (self, cfg)

    # --------------------------------------------------------------------------
    #
    def initialize_child(self):

        self.declare_input (rp.EXECUTING_PENDING, rp.AGENT_EXECUTING_QUEUE)
        self.declare_worker(rp.EXECUTING_PENDING, self.work)

        self.declare_output(rp.AGENT_STAGING_OUTPUT_PENDING, rp.AGENT_STAGING_OUTPUT_QUEUE)

        self.declare_publisher ('unschedule', rp.AGENT_UNSCHEDULE_PUBSUB)
        self.declare_publisher ('state',      rp.AGENT_STATE_PUBSUB)

        # all components use the command channel for control messages
        self.declare_publisher ('command', rp.AGENT_COMMAND_PUBSUB)
        self.declare_subscriber('command', rp.AGENT_COMMAND_PUBSUB, self.command_cb)

        self._cancel_lock    = threading.RLock()
        self._cus_to_cancel  = list()
        self._watch_queue    = Queue.Queue ()

        self._pilot_id = self._cfg['pilot_id']

        self.task_map = {}

        # run watcher thread
        self._terminate = threading.Event()
        self._watcher   = threading.Thread(target=self._watch, name="Watcher")
        self._watcher.daemon = True
        self._watcher.start ()

        # The AgentExecutingComponent needs the LaunchMethods to construct
        # commands.
        if not (self._cfg['task_launch_method'] ==
                self._cfg['mpi_launch_method'] ==
                "ORTE_LIB"):
            raise Exception("ORTE_LIB spawner only works with ORTE_LIB LM's.")

        self._task_launcher = rp.agent.LM.create(
            name   = "ORTE_LIB",
            cfg    = self._cfg,
            logger = self._log)

        self._orte_initialized = False

        # communicate successful startup
        self.publish('command', {'cmd' : 'alive', 'arg' : self.cname})

        self._cu_environment = self._populate_cu_environment()

        self.tmpdir = tempfile.gettempdir()

    # --------------------------------------------------------------------------
    #
    def finalize_child(self):

        # terminate watcher thread
        self._terminate.set()
        # self._watcher.join()

        # communicate finalization
        self.publish('command', {'cmd' : 'final',
                                 'arg' : self.cname})

    # --------------------------------------------------------------------------
    #
    def command_cb(self, topic, msg):

        cmd = msg['cmd']
        arg = msg['arg']

        if cmd == 'cancel_unit':

            self._log.info("cancel unit command (%s)" % arg)
            with self._cancel_lock:
                self._cus_to_cancel.append(arg)

    # --------------------------------------------------------------------------
    #
    def _populate_cu_environment(self):
        """Derive the environment for the cu's from our own environment."""

        # Get the environment of the agent
        new_env = copy.deepcopy(os.environ)

        #
        # Mimic what virtualenv's "deactivate" would do
        #
        old_path = new_env.pop('_OLD_VIRTUAL_PATH', None)
        if old_path:
            new_env['PATH'] = old_path

        old_home = new_env.pop('_OLD_VIRTUAL_PYTHONHOME', None)
        if old_home:
            new_env['PYTHON_HOME'] = old_home

        old_ps = new_env.pop('_OLD_VIRTUAL_PS1', None)
        if old_ps:
            new_env['PS1'] = old_ps

        new_env.pop('VIRTUAL_ENV', None)

        # Remove the configured set of environment variables from the
        # environment that we pass to Popen.
        for e in new_env.keys():
            env_removables = list()
            if self._task_launcher:
                env_removables += self._task_launcher.env_removables
            for r in  env_removables:
                if e.startswith(r):
                    new_env.pop(e, None)

        return new_env

    # --------------------------------------------------------------------------
    #
    def work(self, cu):

        if not self._orte_initialized:
            self._log.debug("ORTE not yet initialized!")
            ret = self.init_orte(cu)
            if ret != 0:
                self._log.debug("ORTE initialisation failed!")
            else:
                self._log.debug("ORTE initialisation succeeded!")

        try:
            launcher = self._task_launcher

            if not launcher:
                raise RuntimeError("no launcher (mpi=%s)" % cu['description']['mpi'])

            self._log.debug("Launching unit with %s (%s).", launcher.name, launcher.launch_command)

            assert(cu['opaque_slots']) # FIXME: no assert, but check
            self._prof.prof('exec', msg='unit launch', uid=cu['_id'])

            # Start a new subprocess to launch the unit
            self.spawn(launcher=launcher, cu=cu)

        except Exception as e:
            # append the startup error to the units stderr.  This is
            # not completely correct (as this text is not produced
            # by the unit), but it seems the most intuitive way to
            # communicate that error to the application/user.
            self._log.exception("error running CU: %s", str(e))
            cu['stderr'] += "\nPilot cannot start compute unit:\n%s\n%s" \
                            % (str(e), traceback.format_exc())

            # Free the Slots, Flee the Flots, Ree the Frots!
            if cu['opaque_slots']:
                self.publish('unschedule', cu)

            self.advance(cu, rp.FAILED, publish=True, push=False)

    # --------------------------------------------------------------------------
    #
    def unit_spawned_cb(self, task):

        cu = self.task_map[task]

        cu['started'] = rpu.timestamp()

        cu_id = cu['_id']
        self._log.debug("[%s] Unit %s has spawned." % (time.ctime(), cu_id))
        self._prof.prof('passed', msg="ExecWatcher picked up unit", uid=cu_id)

        self.advance(cu, rp.EXECUTING, publish=True, push=False)

    # --------------------------------------------------------------------------
    #
    def unit_completed_cb(self, task, exit_code):

        timestamp = rpu.timestamp()

        cu = self.task_map[task]
        del self.task_map[task]

        self._prof.prof('exec', msg='execution complete', uid=cu['_id'])

        # we have a valid return code -- unit is final
        self._log.info("Unit %s has return code %s.", cu['_id'], exit_code)

        cu['exit_code'] = exit_code
        cu['finished']  = timestamp

        # Free the Slots, Flee the Flots, Ree the Frots!
        self.publish('unschedule', cu)

        if exit_code != 0:
            # The unit failed - fail after staging output
            self._prof.prof('final', msg="execution failed", uid=cu['_id'])
            cu['target_state'] = rp.FAILED

        else:
            # The unit finished cleanly, see if we need to deal with
            # output data.  We always move to stageout, even if there are no
            # directives -- at the very least, we'll upload stdout/stderr
            self._prof.prof('final', msg="execution succeeded", uid=cu['_id'])
            cu['target_state'] = rp.DONE

        # TODO: push=False because this is a callback?
        self.advance(cu, rp.AGENT_STAGING_OUTPUT_PENDING, publish=True, push=True)


    # --------------------------------------------------------------------------
    #
    def init_orte(self, cu):
        # TODO: it feels as a hack to get the DVM URI from the first CU

        opaque_slots = cu['opaque_slots']

        if 'lm_info' not in opaque_slots:
            raise RuntimeError('No lm_info to init via %s: %s' \
                               % (self.name, opaque_slots))

        if not opaque_slots['lm_info']:
            raise RuntimeError('lm_info missing for %s: %s' \
                               % (self.name, opaque_slots))

        if 'dvm_uri' not in opaque_slots['lm_info']:
            raise RuntimeError('dvm_uri not in lm_info for %s: %s' \
                               % (self.name, opaque_slots))

        dvm_uri    = opaque_slots['lm_info']['dvm_uri']

        argv_keepalive = [
            ffi.new("char[]", "RADICAL-Pilot"), # Will be stripped off by the library
            ffi.new("char[]", "--hnp"), ffi.new("char[]", str(dvm_uri)),
            ffi.NULL, # Required
        ]
        argv = ffi.new("char *[]", argv_keepalive)
        ret = orte_lib.orte_submit_init(3, argv, ffi.NULL)

        self._myhandle = ffi.new_handle(self)
        self._orte_initialized = True

        return ret

    # --------------------------------------------------------------------------
    #
    def spawn(self, launcher, cu):


        self._prof.prof('spawn', msg='unit spawn', uid=cu['_id'])

        if False:
            cu_tmpdir = '%s/%s' % (self.tmpdir, cu['_id'])
        else:
            cu_tmpdir = cu['workdir']

        rec_makedir(cu_tmpdir)

        # TODO: pre_exec
        #     # Before the Big Bang there was nothing
        #     if cu['description']['pre_exec']:
        #         pre_exec_string = ''
        #         if isinstance(cu['description']['pre_exec'], list):
        #             for elem in cu['description']['pre_exec']:
        #                 pre_exec_string += "%s\n" % elem
        #         else:
        #             pre_exec_string += "%s\n" % cu['description']['pre_exec']
        #         # Note: extra spaces below are for visual alignment
        #         launch_script.write("# Pre-exec commands\n")
        #         if 'RADICAL_PILOT_PROFILE' in os.environ:
        #             launch_script.write("echo pre  start `%s` >> %s/PROF\n" % (cu['gtod'], cu_tmpdir))
        #         launch_script.write(pre_exec_string)
        #         if 'RADICAL_PILOT_PROFILE' in os.environ:
        #             launch_script.write("echo pre  stop `%s` >> %s/PROF\n" % (cu['gtod'], cu_tmpdir))

        # TODO: post_exec
        # # After the universe dies the infrared death, there will be nothing
        # if cu['description']['post_exec']:
        #     post_exec_string = ''
        #     if isinstance(cu['description']['post_exec'], list):
        #         for elem in cu['description']['post_exec']:
        #             post_exec_string += "%s\n" % elem
        #     else:
        #         post_exec_string += "%s\n" % cu['description']['post_exec']
        #     launch_script.write("# Post-exec commands\n")
        #     if 'RADICAL_PILOT_PROFILE' in os.environ:
        #         launch_script.write("echo post start `%s` >> %s/PROF\n" % (cu['gtod'], cu_tmpdir))
        #     launch_script.write('%s\n' % post_exec_string)
        #     if 'RADICAL_PILOT_PROFILE' in os.environ:
        #         launch_script.write("echo post stop  `%s` >> %s/PROF\n" % (cu['gtod'], cu_tmpdir))



        # The actual command line, constructed per launch-method
        try:
            orte_command, task_command = launcher.construct_command(cu, None)
        except Exception as e:
            msg = "Error in spawner (%s)" % e
            self._log.exception(msg)
            raise RuntimeError(msg)

        # Construct arguments to submit_job
        arg_list = []

        # Take the orte specific commands and split them
        for arg in orte_command.split():
            arg_list.append(ffi.new("char[]", str(arg)))

        # Set the working directory
        arg_list.append(ffi.new("char[]", "--wdir"))
        arg_list.append(ffi.new("char[]", str(cu_tmpdir)))

        # Set RP environment variables
        rp_envs = [
            "RP_SESSION_ID=%s" % self._cfg['session_id'],
            "RP_PILOT_ID=%s" % self._cfg['pilot_id'],
            "RP_AGENT_ID=%s" % self._cfg['agent_name'],
            "RP_SPAWNER_ID=%s" % self.cname,
            "RP_UNIT_ID=%s" % cu['_id']
        ]
        for env in rp_envs:
            arg_list.append(ffi.new("char[]", "-x"))
            arg_list.append(ffi.new("char[]", str(env)))

        # Set pre-populated environment variables
        if self._cu_environment:
            for key,val in self._cu_environment.iteritems():
                arg_list.append(ffi.new("char[]", "-x"))
                arg_list.append(ffi.new("char[]", "%s=%s" % (key, val)))

        # Set environment variables specified for this CU
        if cu['description']['environment']:
            for key,val in cu['description']['environment'].iteritems():
                arg_list.append(ffi.new("char[]", "-x"))
                arg_list.append(ffi.new("char[]", "%s=%s" % (key, val)))

        # Save retval of actual CU application (in case we have post-exec)
        # TODO: add the exit $RETVAL somewhere
        task_command += "; RETVAL=$?"

        # Wrap in (sub)shell for output redirection
        arg_list.append(ffi.new("char[]", "sh"))
        arg_list.append(ffi.new("char[]", "-c"))
        if 'RADICAL_PILOT_PROFILE' in os.environ:
            task_command = "echo script start_script `%s` >> %s/PROF; " % (cu['gtod'], cu_tmpdir) + \
                      "echo script after_cd `%s` >> %s/PROF; " % (cu['gtod'], cu_tmpdir) + \
                      task_command + \
                      "; echo script after_exec `%s` >> %s/PROF\n" % (cu['gtod'], cu_tmpdir)
        arg_list.append(ffi.new("char[]", str("(%s) 1>%s 2>%s; exit $RETVAL" % (str(task_command), cu['stdout_file'], cu['stderr_file']))))

        self._log.debug("Launching unit %s via %s %s", cu['_id'], orte_command, task_command)

        # NULL termination, required by ORTE
        arg_list.append(ffi.NULL)
        argv = ffi.new("char *[]", arg_list)
        self._prof.prof('command', msg='launch command constructed', uid=cu['_id'])

        # Submit to the DVM!
        try:
            task = orte_lib.orte_submit_job(argv, orte_lib.launch_cb, self._myhandle, orte_lib.finish_cb, self._myhandle)
        except Exception as e:
            raise Exception("submit job failed: %s" % str(e))

        self._prof.prof('spawn', msg='spawning passed to orte', uid=cu['_id'])

        # Record the mapping of ORTE index to CU
        self.task_map[task] = cu

        self._log.debug("Task %d submitted!", task)

        # Put on the watch queue list to enable the unit to be canceled
        self._watch_queue.put(cu)

    # --------------------------------------------------------------------------
    #
    def _watch(self):

        self._prof.prof('run', uid=self._pilot_id)
        try:
            while not self._terminate.is_set():
                try:
                    cu = self._watch_queue.get_nowait()

                    if cu['_id'] in self._cus_to_cancel:

                        # We got a request to cancel this cu
                        # TODO: What is the equivalent on ORTE?
                        # cu['proc'].kill()

                        with self._cancel_lock:
                            self._cus_to_cancel.remove(cu['_id'])

                        self._prof.prof('final', msg="execution canceled", uid=cu['_id'])

                        self.publish('unschedule', cu)
                        self.advance(cu, rp.CANCELED, publish=True, push=False)

                except Queue.Empty:
                    # nothing found -- no problem
                    time.sleep(1)

        except Exception as e:
            self._log.exception("Error in ExecWorker watch loop (%s)" % e)
            # FIXME: this should signal the ExecWorker for shutdown...