from multiprocessing import Process, Pool
import os
from datetime import datetime
import copy
import logging
import socket

import yaml
import redcap

import dax
from dax import DAX_Settings
from dax.launcher import BUILD_SUFFIX
from dax import log

# TODO: number of multiprocs - determine how many are available base on how 
# many locks exist when dax manager starts. so it e.g. 2 locks exists when we
# start, only allow n - 2 in the pool

# TODO: archive old logs

# TODO: only run launch and update if there are open jobs


DAX_SETTINGS = DAX_Settings()

LOGGER = log.setup_debug_logger('manager', None)


def get_this_instance():
    # build the instance name
    this_host = socket.gethostname().split('.')[0]
    this_user = os.environ['USER']
    return '{}@{}'.format(this_user, this_host)


def clean_lockfiles():
    lock_dir = os.path.join(DAX_SETTINGS.get_results_dir(), 'FlagFiles')
    lock_list = os.listdir(lock_dir)

    # Make full paths
    lock_list = [os.path.join(lock_dir, f) for f in lock_list]

    # Check each lock file
    for file in lock_list:
        LOGGER.debug('checking lock file:{}'.format(file))
        check_lockfile(file)


def check_lockfile(file):
    # Try to read host-PID from lockfile
    try:
        with open(file, 'r') as f:
            line = f.readline()

        host, pid = line.split('-')
        pid = int(pid)

        # Compare host to current host
        this_host = socket.gethostname().split('.')[0]
        if host != this_host:
            LOGGER.debug('different host, cannot check PID:{}', format(file))
        elif pid_exists(pid):
            LOGGER.debug('host matches and PID still exists')
        else:
            LOGGER.debug('host matches and PID not running, deleting lockfile')
            os.remove(file)
    except IOError:
        LOGGER.debug('failed to read from lock file:{}'.format(file))
    except ValueError:
        LOGGER.debug('failed to parse lock file:{}'.format(file))


def pid_exists(pid):
    if pid < 0:
        return False   # NOTE: pid == 0 returns True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:   # errno.ESRCH
        return False  # No such process
    except PermissionError:  # errno.EPERM
        return True  # Operation not permitted (i.e., process exists)
    else:
        return True  # no error, we can send a signal to the process


def is_locked(settings_path):
    lockfile_prefix = os.path.splitext(os.path.basename(settings_path))[0]
    flagfile = os.path.join(
        DAX_SETTINGS.get_results_dir(),
        'FlagFiles',
        '{}_{}'.format(lockfile_prefix, BUILD_SUFFIX))

    LOGGER.debug('checking for flag file:{}'.format(flagfile))
    return os.path.isfile(flagfile)


class DaxManagerError(Exception):
    """Custom exception raised with dax manager."""
    def __init__(self, message):
        Exception.__init__(self, 'Error with dax manager:{}'.format(message))


class DaxProjectSettings(object):
    def __init__(self):
        self.general = {}
        self.processors = []
        self.modules = []
        self.projects = []

    def dump(self):
        return {
            **self.general,
            'modules': self.modules,
            'yamlprocessors': self.processors,
            'projects': self.projects}

    def set_general(self, general):
        # TODO confirm it has required elements
        self.general = general

    def add_processor(self, processor):
        # TODO confirm it has required elements and maintain unique names
        self.processors.append(processor)

    def add_module(self, module):
        # TODO confirm it has required elements and maintain unique names
        self.modules.append(module)

    def add_project(self, project):
        # TODO confirm it has required elements and maintain unique names
        self.projects.append(project)

    def processor_names(self):
        return [x['name'] for x in self.processors]

    def module_names(self):
        return [x['name'] for x in self.modules]

    def module_byname(self, name):
        mod = None
        for m in self.modules:
            if m['name'] == name:
                mod = m
                break

        return mod

    def processor_byname(self, name):
        proc = None
        for p in self.processors:
            if p['name'] == name:
                proc = p
                break

        return proc


class DaxProjectSettingsManager(object):
    RCDATEFORMAT = '%Y-%m-%d %H:%M:%S'

    MOD_PREFIX = 'module_'

    PROC_PREFIX = 'processor_'

    FILE_HEADER = '''# This file generated by dax manager.
# Edits should be made on REDCap.
'''

    # initialize with REDCap url/key
    def __init__(
            self, redcap_url, redcap_key, instance_settings,
            local_dir, general_form='general'):

        self._general_form = general_form
        self._local_dir = local_dir
        self._instance_settings = instance_settings

        # Initialize redcap project
        self._redcap = redcap.Project(redcap_url, redcap_key)

    def write_each(self):
        now = datetime.now()

        # First load the settings from our defaults project
        default_settings = self.load_defaults(DaxProjectSettings())

        for name in self.project_names():
            settings = copy.deepcopy(default_settings)

            LOGGER.info('Loading project: ' + name)
            project = self.load_project(settings, name)
            settings.add_project(project)

            # Write project file
            filename = os.path.join(
                self._local_dir, 'settings-{}.yaml'.format(name))
            self.write_settings_file(filename, settings, now)

    def write_settings_file(self, filename, settings, timestamp):
        LOGGER.info('Writing settings to file:' + filename)
        with open(filename, 'w') as f:
            f.write(self.FILE_HEADER)
            f.write('# {}\n'.format(str(timestamp)))
            yaml.dump(
                settings.dump(),
                f,
                sort_keys=False,
                default_flow_style=False,
                explicit_start=True)

    def general_defaults(self):
        rec = {}

        ins = self._instance_settings
        rec['processorlib'] = ins['main_processorlib']
        rec['modulelib'] = ins['main_modulelib']
        rec['singularity_imagedir'] = ins['main_singularityimagedir']
        rec['attrs'] = {}
        rec['attrs']['queue_limit'] = int(ins['main_queuelimit'])
        rec['attrs']['job_email_options'] = ins['main_jobemailoptions']
        rec['attrs']['xnat_host'] = ins['main_xnathost']

        return rec

    def load_defaults(self, settings):
        # First set the general section
        settings.set_general(self.general_defaults())

        return settings

    def load_module_names(self):
        p = DaxProjectSettingsManager.MOD_PREFIX
        return [x.split(p)[1] for x in self._redcap.forms if x.startswith(p)]

    def load_processor_names(self):
        p = DaxProjectSettingsManager.PROC_PREFIX
        return [x.split(p)[1] for x in self._redcap.forms if x.startswith(p)]

    def load_module_record(self, module, project):
        prefix = DaxProjectSettingsManager.MOD_PREFIX
        form = prefix + module
        rc_rec = self._redcap.export_records(
            forms=[form], records=[project])[0]
        dax_rec = {'name': module}

        # Find module prefix
        key = [x for x in rc_rec.keys() if x.endswith('_file')]
        if len(key) > 1:
            msg = 'multiple _file keys for module:{}'.format(module)
            raise DaxManagerError(msg)
        elif len(key) == 0:
            msg = 'no _file key for module:{}'.format(module)
            raise DaxManagerError(msg)

        file_key = key[0]
        key_prefix = file_key.split('_file')[0]

        # Get the filepath
        dax_rec['filepath'] = rc_rec[file_key]

        # Parse arguments
        if rc_rec[key_prefix + '_args']:
            rlist = rc_rec[key_prefix + '_args'].strip().split('\r\n')
            rdict = {}
            for arg in rlist:
                key, val = arg.split(':', 1)
                rdict[key] = val.strip()

            dax_rec['arguments'] = rdict

        return dax_rec

    def load_processor_record(self, processor, project):
        prefix = DaxProjectSettingsManager.PROC_PREFIX
        form = prefix + processor
        rc_rec = self._redcap.export_records(
            forms=[form], records=[project])[0]
        dax_rec = {'name': processor}

        # Find processor prefix
        key = [x for x in rc_rec.keys() if x.endswith('_file')]
        if len(key) > 1:
            msg = 'multiple _file keys for proc:{}'.format(processor)
            raise DaxManagerError(msg)
        elif len(key) == 0:
            msg = 'no _file key found for proc:{}'.format(processor)
            raise DaxManagerError(msg)

        file_key = key[0]
        key_prefix = file_key.split('_file')[0]

        # Get the filepath
        dax_rec['filepath'] = rc_rec[file_key]

        # Check for arguments
        if rc_rec[key_prefix + '_args']:
            rlist = rc_rec[key_prefix + '_args'].strip().split('\r\n')
            rdict = {}
            for arg in rlist:
                key, val = arg.split(':', 1)
                rdict[key] = val.strip()

            dax_rec['arguments'] = rdict

        return dax_rec

    def is_enabled_module(self, module, project):
        prefix = DaxProjectSettingsManager.MOD_PREFIX
        form = prefix + module
        rec = self._redcap.export_records(forms=[form], records=[project])[0]
        complete = rec[form + '_complete']
        return (complete == '2')

    def is_enabled_processor(self, processor, project):
        prefix = DaxProjectSettingsManager.PROC_PREFIX
        form = prefix + processor
        rec = self._redcap.export_records(forms=[form], records=[project])[0]
        complete = rec[form + '_complete']
        return (complete == '2')

    def project_names(self):
        complete_field = self._general_form + '_complete'
        instance_field = 'gen_daxinstance'
        name_field = 'project_name'

        # Get projects from REDCap
        plist = self._redcap.export_records(
            fields=[name_field, instance_field, complete_field],
            raw_or_label='label')

        # Filter to only include projects for this instance
        this_instance = get_this_instance()
        plist = [x for x in plist if x[instance_field] == this_instance]

        # Return project names that are enabled
        return [x[name_field] for x in plist if x[complete_field] == 'Complete']

    def load_project(self, settings, project):
        proj_proc = []
        proj_mod = []

        # Get the project modules
        mod_names = self.load_module_names()
        for name in mod_names:
            if not self.is_enabled_module(name, project):
                continue

            # Make a new module
            mod = self.load_module_record(name, project)
            mod['name'] = name

            # Add the custom module to our settings
            settings.add_module(mod)

            # Append it to list for this project
            proj_mod.append(name)

        # Get the project processors
        proc_names = self.load_processor_names()
        for name in proc_names:
            if not self.is_enabled_processor(name, project):
                continue

            # Make a new custom processor
            proc = self.load_processor_record(name, project)
            proc['name'] = name

            # Add the custom module to our settings
            settings.add_processor(proc)

            # Append it to list for this project
            proj_proc.append(name)

        return {
            'project': project,
            'modules': ','.join(proj_mod),
            'yamlprocessors': ','.join(proj_proc)}

    def delete_disabled(self):
        # Get disabled project names from REDCap
        field = self._general_form + '_complete'
        rlist = self._redcap.export_records(fields=['project_name', field])
        disabled_list = [x['project_name'] for x in rlist if x[field] != '2']

        # Delete disabled project settings files
        for name in disabled_list:
            filename = os.path.join(
                self._local_dir, 'settings-{}.yaml'.format(name))
            if os.path.exists(filename):
                LOGGER.info('deleting disabled project:{}'.format(filename))
                os.remove(filename)

        return

    def get_last_start_time(self, project):
        rec = self._redcap.export_records(
            fields=['build_laststarttime'],
            records=[project])[0]

        return rec['build_laststarttime']

    def get_last_run(self, project):
        rec = self._redcap.export_records(
            fields=[
                'build_lastcompletestarttime', 'build_lastcompletefinishtime'],
            records=[project])[0]

        last_start = rec['build_lastcompletestarttime']
        last_finish = rec['build_lastcompletefinishtime']
        last_run = None

        if last_start != '' and last_finish != '' and last_start < last_finish:
            last_run = datetime.strptime(last_start, self.RCDATEFORMAT)

        return last_run

    def set_last_build_start(self, project):
        last_start = datetime.strftime(datetime.now(), self.RCDATEFORMAT)

        rec = {
            'project_name': project,
            'build_laststarttime': last_start,
            'build_status_complete': '1'}

        LOGGER.info('set last build start: project={}, {}'.format(
            project,
            last_start))

        try:
            response = self._redcap.import_records([rec])
            assert 'count' in response
        except AssertionError as err:
            err = 'redcap import failed'
            LOGGER.info(err)
            raise DaxManagerError(err)
        except Exception as e:
            err = 'connection to REDCap interrupted'
            LOGGER.info(e)
            raise DaxManagerError(err)

    def set_last_build_complete(self, project):
        last_finish = datetime.strftime(datetime.now(), self.RCDATEFORMAT)
        last_start = self.get_last_start_time(project)
        last_duration = self.duration(last_start, last_finish)

        rec = {
            'project_name': project,
            'build_lastcompletestarttime': last_start,
            'build_lastcompletefinishtime': last_finish,
            'build_lastcompleteduration': last_duration,
            'build_status_complete': '2'}

        LOGGER.info('set last build: project={}, start={}, finish={}'.format(
            project,
            last_start,
            last_finish))

        try:
            response = self._redcap.import_records([rec])
            assert 'count' in response
        except AssertionError:
            err = 'redcap import failed'
            raise DaxManagerError(err)
        except Exception:
            err = 'connection to REDCap interrupted'
            raise DaxManagerError(err)

    def duration(self, start_time, finish_time):
        try:
            time_delta = (datetime.strptime(finish_time, self.RCDATEFORMAT) -
                          datetime.strptime(start_time, self.RCDATEFORMAT))
            secs = time_delta.total_seconds()
            hours = int(secs // 3600)
            mins = int((secs % 3600) // 60)
            if hours > 0:
                duration = '{} hrs {} mins'.format(hours, mins)
            else:
                duration = '{} mins'.format(mins)
        except Exception as e:
            LOGGER.debug(e)
            duration = None

        return duration


class DaxManager(object):
    FDATEFORMAT = '%Y%m%d-%H%M%S'

    def __init__(self, api_url, api_key_instances, api_key_projects):

        # Load settings for this instance
        self.instance_settings = self.load_instance_settings(
            api_url, api_key_instances)

        LOGGER.debug(self.instance_settings)

        self.settings_dir = self.instance_settings['main_projectsettingsdir']
        self.log_dir = self.instance_settings['main_logdir']

        # Create our settings manager and update our settings directory
        self.settings_manager = DaxProjectSettingsManager(
            api_url, api_key_projects,
            self.instance_settings, self.settings_dir)

        self.refresh_settings()

    def load_instance_settings(
            self,  redcap_url, redcap_key, main_form='main'):

        self._main_form = main_form

        # Initialize redcap project
        self._redcap = redcap.Project(redcap_url, redcap_key)

        # get this instance name
        instance_name = get_this_instance()
        LOGGER.debug('instance={}'.format(instance_name))

        # Return the reocrd associate with this instance_name
        return self._redcap.export_records(records=[instance_name])[0]

    def refresh_settings(self):
        # Delete existing settings files
        for filename in self.list_settings_files(self.settings_dir):
            os.remove(filename)

        # Write settings files
        self.settings_manager.write_each()

        # Load settings files
        self.settings_list = self.list_settings_files(self.settings_dir)
        LOGGER.info(self.settings_list)

    def list_settings_files(self, settings_dir):
        slist = os.listdir(settings_dir)

        # Make full paths
        slist = [os.path.join(settings_dir, f) for f in slist]

        # Only yaml files
        slist = [f for f in slist if f.endswith('.yaml') and os.path.isfile(f)]

        return slist

    def project_from_settings(self, settings_file):
        proj = settings_file.split('settings-')[1].split('.yaml')[0]
        return proj

    def log_name(self, runtype, project, timestamp):
        log = os.path.join(self.log_dir, '{}_{}_{}.log'.format(
            runtype, project, datetime.strftime(timestamp, self.FDATEFORMAT)))

        return log

    def queue_builds(self, build_pool, settings_list):
        # TODO: sort builds by how long we expect them to take,
        # shortest to longest

        # Array to store result accessors
        build_results = [None]*len(settings_list)

        # Run each
        for i, settings_path in enumerate(settings_list):
            proj = self.project_from_settings(settings_path)
            log_path = self.log_name('build', proj, datetime.now())
            last_run = self.get_last_run(proj)

            LOGGER.info('SETTINGS:{}'.format(settings_path))
            LOGGER.info('PROJECT:{}'.format(proj))
            LOGGER.info('LOG:{}'.format(log_path))
            LOGGER.info('LASTRUN:' + str(last_run))
            build_results[i] = build_pool.apply_async(
                self.run_build, [proj, settings_path, log_path, last_run])

        return build_results

    def run(self):
        # Build
        num_build_threads = 10
        build_pool = Pool(processes=num_build_threads)
        build_results = self.queue_builds(build_pool, self.settings_list)
        build_pool.close()  # Close the pool, I dunno if this matters

        # Update
        LOGGER.info('updating')
        for settings_path in self.settings_list:
            proj = self.project_from_settings(settings_path)

            LOGGER.info('updating jobs:' + proj)
            log = self.log_name('update', proj, datetime.now())
            self.run_update(settings_path, log)

        # Launch - report to log if locked
        LOGGER.info('launching')
        for settings_path in self.settings_list:
            proj = self.project_from_settings(settings_path)

            LOGGER.info('launching jobs:' + proj)
            log = self.log_name('launch', proj, datetime.now())
            self.run_launch(settings_path, log)

        # Upload - report to log if locked
        log = self.log_name('upload', '', datetime.now())
        upload_process = Process(
            target=self.run_upload,
            args=(None, log))
        LOGGER.info('starting upload')
        upload_process.start()
        LOGGER.info('waiting for upload')
        upload_process.join()
        LOGGER.info('upload complete')

        # Wait for builds to finish
        LOGGER.info('waiting for builds to finish')
        build_pool.join()

    def run_build(self, project, settings_file, log_file, lastrun):
        # Check for existing lock
        if is_locked(settings_file):
            LOGGER.warn('cannot build, lock exists:{}'.format(settings_file))
            # TODO: check if it's really running, if not send a notification
        else:
            # dax.bin.build expects a map of project to lastrun
            proj_lastrun = {project: lastrun}

            LOGGER.info('run_build:{},{}'.format(project, lastrun))
            self.set_last_build_start(project)
            dax.bin.build(
                settings_file, log_file, debug=True, proj_lastrun=proj_lastrun)

            # TODO: check for errors in log file and set to RED if any found,
            # also could upload last log file

            self.set_last_build_complete(project)
            LOGGER.info('run_build:done:{}'.format(project))

    def set_last_build_start(self, project):
        self.settings_manager.set_last_build_start(project)

    def set_last_build_complete(self, project):
        self.settings_manager.set_last_build_complete(project)

    def get_last_run(self, project):
        return self.settings_manager.get_last_run(project)

    def run_launch(self, settings_file, log_file):
        dax.bin.launch_jobs(settings_file, log_file, debug=True)
        logging.getLogger('dax').handlers = []

    def run_update(self, settings_file, log_file):
        dax.bin.update_tasks(settings_file, log_file, debug=True)
        logging.getLogger('dax').handlers = []

    def run_upload(self, settings_file, log_file):
        dax.dax_tools_utils.upload_tasks(log_file, True, settings_file)
        logging.getLogger('dax').handlers = []

    def all_ready(self, results):
        ready = True
        for i, res in enumerate(results):
            if not res.ready():
                LOGGER.debug('not ready:{}'.format(str(i)))
                ready = False

        return ready


if __name__ == '__main__':
    API_URL = os.environ['API_URL']
    API_KEY_P = os.environ['API_KEY_DAX_PROJECTS']
    API_KEY_I = os.environ['API_KEY_DAX_INSTANCES']

    # Clean up existing lock files
    clean_lockfiles()

    # Make our dax manager
    manager = DaxManager(API_URL, API_KEY_I, API_KEY_P)

    # And run it
    manager.run()

    LOGGER.info('ALL DONE!')