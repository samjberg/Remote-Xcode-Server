from sys import set_coroutine_origin_tracking_depth
from environment_setup import _normalize_path_for_compare
from mcp_utils import get_project_name, get_server_dir_path, generate_project_id, get_git_username, ensure_directory_exists, is_git_repo
from flask import request
import os, json, time, subprocess

server_dir_path = get_server_dir_path()
default_projects_dir_name = 'projects'
default_projects_dir_path = os.path.join(server_dir_path, default_projects_dir_name)
projects_dict_filename = 'tracked_projects.csv'
projects_dict_filepath = os.path.join(server_dir_path, projects_dict_filename)
cwd = os.getcwd()
current_project = {}
projects_dict = {}
project_runtime_dir_name = '.remote-xcode-server'
project_runtime_dir_path = ''
known_git_repos_filename = 'known_git_repos.json'
known_git_repos = []


def load_projects_dict() -> dict:
    if not os.path.exists(projects_dict_filepath):
        raise FileNotFoundError(f'Error, file not found at path: {projects_dict_filepath}')

    projects_dict = {}

    with open(projects_dict_filepath, 'r') as f:
        try:
            projects_dict = json.load(f)
        except json.JSONDecodeError as e:
            print(f'Error decoding project list json at path: {projects_dict_filepath}')
            raise e

    return projects_dict

def save_projects_dict(projs_dict: dict=None):
    if projs_dict is None:
        projs_dict = projects_dict
    if not os.path.exists(projects_dict_filepath):
        raise FileNotFoundError(f'Error opening projects dict file at: {projects_dict_filepath}')

    with open(projects_dict_filepath, 'w') as f:
        json.dump(projs_dict, f)

def _ensure_server_dir() -> None:
    ensure_directory_exists(server_dir_path)


def _ensure_projects_dict_file() -> None:
    global projects_dict_filepath
    if not os.path.exists(projects_dict_filepath):
        if not os.path.exists(server_dir_path):
            os.makedirs(server_dir_path)
        #create empty file at projects_dict_filepath
        with open(projects_dict_filepath, 'w') as f:
            f.write('{}\n')

def _ensure_default_projects_dir() -> None:
    ensure_directory_exists(default_projects_dir_path)

def _create_project(project_name:str, project_root_path: str, client_ip:str =''):
    project_id = generate_project_id(project_name)
    timestamp = time.time()
    project = {'id': project_id, 'project_name': project_name, 'project_root_path': project_root_path,
             'tracked_timestamp': timestamp, 'last_command_timestamp': timestamp, 'known_clients': []}
    if client_ip:
        project['known_clients'].append(client_ip)
    return project

def add_project_to_list(project_name: str, project_root_path: str, client_ip: str='', set_as_current_project=False) -> dict:
    global projects_dict
    if not projects_dict:
        projects_dict = load_projects_dict()

    project = _create_project(project_name, project_root_path, client_ip)
    project_id = project.get('id', '')
    if not project_id:
        raise RuntimeError('Error: newly generated project id is Falsey.  project: {project}')

    projects_dict[project_id] = project

    if set_as_current_project:
        set_current_project(project_id, project_name)

    return project



def remove_project_from_list(project_id:str = '', project_name: str = '') -> None:
    global projects_dict
    if (not project_id) and (not project_name):
        raise ValueError('Must provide either project_id (preferred) or project_name')

    loaded_dict = load_projects_dict()
    project_set = set(projects_dict.keys())
    loaded_set = set(loaded_dict.keys())
    project_set.symmetric_difference(loaded_set)
    if project_set:
        for loaded_only_key in loaded_set.difference(project_set):
            projects_dict[loaded_only_key] = loaded_dict[loaded_only_key]

        for project_only_key in project_set.difference(loaded_set):
            loaded_dict[project_only_key] = projects_dict[project_only_key]

    if not project_id:
        project_id = generate_project_id(project_name)

    if project_id in projects_dict:
        del projects_dict[project_id]
    else:
        #in case there is somehow some issue with the id being generated inconsistently, but the entry really does exist
        #we iterate through to manually check for a project name match
        id_to_remove = ''
        for proj_id, project in projects_dict.items():
            if project_name == project.get('project_name', ''):
                id_to_remove = proj_id
                break
        if id_to_remove:
            del projects_dict[id_to_remove]

def set_current_project(project_id: str='', project_name: str='', project: dict = None):
    '''Sets current project by either id and/or name'''
    global current_project, project_runtime_dir_path
    if project:
        current_project = project
    elif project_id and project_name:
        if project_id in projects_dict:
            if 'project_name' in projects_dict[project_id]:
                current_project = projects_dict[project_id]
            else:
                err_msg = '''ERROR ERROR ERROR!!!  PAY ATTENTION TO THIS!!!
                               The project_id was found, but its dict had no stored project_name'''
                raise ValueError(err_msg)
        else:
            raise RuntimeError(f'Project not found.  Given name: {project_name}\nGiven id: {project_id}')
    elif project_id:
        #project_name is ''
        proj_dct = projects_dict.get(project_id, None)
        if not proj_dct:
            loaded_projects_dct = load_projects_dict()
            if not project_id in loaded_projects_dct.keys():
                raise KeyError(f'No project found with project_id: {project_id}')

            proj_dct = loaded_projects_dct.get(project_id, {})
            #update in memory projects dict with all projects it is missing that are contained in the projects file
            for proj_id in set(loaded_projects_dct).difference(projects_dict):
                projects_dict[proj_id] = loaded_projects_dct[proj_id]

        if proj_dct:
            current_project = proj_dct
        else:
            raise RuntimeError('Error: Cannot set current project.  Only project_id was given, and the project could not be found')

    elif project_name:
        #project_id is ''
        for proj_id, proj in projects_dict.items():
            if proj.get('project_name', '') == project_name:
                current_project = projects_dict[proj_id]
                # current_project['id'] = proj_id
                # current_project['project_name'] = project_name
                break
    else:
        raise RuntimeWarning('Warning: Called set_current_project with no arguments')

    if current_project:
        project_root = current_project.get('project_root_path', '')
        if not project_root:
            raise ValueError('Error, invalid project_root_path: {project_root}')
        project_runtime_dir_path = os.path.join(project_root, project_runtime_dir_name)

    #verify that project_runtime_dir_path (now that it has been computed) exists, if not, create it
    ensure_directory_exists(project_runtime_dir_path)




def _determine_git_username():
    """For use when first cloning a repo to the server, and the repo's username is not necessarily known.
       Goes through all known projects and counts up the most common username (in most cases there will only be 1 username)
       and returns that username.  This function makes use of get_git_username, which directly calls "git config --list"
    """
    found_usernames = {}
    for _, project in projects_dict.items():
        project_root = project.get('project_root_path', '')
        if project_root:
            username = get_git_username(project_root)
            if username:
                if username in found_usernames.keys():
                    found_usernames[username] += 1
                else:
                    found_usernames[username] = 1

    highest_count = 0
    highest_count_username = ''

    for username, count in found_usernames.items():
        if count > highest_count:
            highest_count = count
            highest_count_username = username

    return highest_count_username


def _get_project_by_id(project_id: str) -> dict:
    project = projects_dict.get(project_id, '')
    if not project:
        loaded_projects_dict = load_projects_dict()
        project = loaded_projects_dict.get(project_id, '')
    return project


def _get_project_by_name(project_name: str) -> dict:
    for project_id, project in projects_dict.items():
        if project.get('project_name') == project_name:
            return project

    for project_id, project in load_projects_dict().items():
        if project.get('project_name') == project_name:
            return project

    #project wasn't found by name in projects_dict or in the projects file
    #final fallback to searching for a name match in known_git_repos
    normed_project_name = _normalize_path_for_compare(project_name)
    for git_repo_path in known_git_repos:
        unnormed_name = os.path.split(git_repo_path)[1]
        git_repo_name = _normalize_path_for_compare(unnormed_name)
        if normed_project_name == git_repo_name:
            #found project
            project_root = git_repo_path
            #create the project and add it to tracked projects
            project = add_project_to_list(project_name, project_root)
            return project

    return {}


def get_project(project_id:str = '', project_name:str = '') -> dict:
    if project_id:
        return _get_project_by_id(project_id)
    elif project_name:
        return _get_project_by_name(project_name)
    return {}


#this function is called by the @app.before_request decorated function
def handle_project_context():
    global projects_dict, current_project, cwd
    if request.path == '/':
        return None
    project_name = request.args.get('project_name', '')
    if not project_name:
        return 'Missing required query parameter: project_name'
    project = get_project(project_name=project_name)
    now = time.time()
    if project:
        #update projects dict
        project_id = project['id']
        #project not in projects_dict or in the saved projects_dict file
        if project_id not in projects_dict:
            #this means that project_id was found in the projects file, but not in in-memory projects_dict
            projects_dict[project_id] = project
        else:
            # this means that project_id was found in
            loaded_projects_dict = load_projects_dict()
            if project_id not in loaded_projects_dict:
                save_projects_dict(projects_dict)

        cwd = project['project_root_path']
        projects_dict[project_id] = project
        set_current_project(project_id=project_id, project_name=project_name)
        project['last_command_timestamp'] = now
        save_projects_dict()
        return

    client_ip = str(request.remote_addr)
    project_path = ''
    project_id = generate_project_id(project_name)
    if project_id in projects_dict.keys():
        set_current_project(project=projects_dict[project_id])
    elif project_id:
        #project_id is not in projects_dict (the project is not tracked yet), but there is a project_id, meaning there was a non_empty name
        #search projects dir, each project directory within it is named its project id (NOT the project's actual name)
        found_project = False
        for proj_id in os.listdir(default_projects_dir_path):
            if proj_id == project_id:
                #this is a very weird case to imagine, but it could happen.  Where I guess the user has manually placed a project
                #inside of rxs's default project directory where it places projects that it doesn't have a given path for
                project_path = os.path.join(default_projects_dir_path, proj_id)
                add_project_to_list(project_name, project_path, client_ip)
                found_project = True
                set_current_project(project_id=project_id, project_name=project_name)
                break

        if not found_project:
            #project not found, manually create it in default projects dir, and add to projects_dict
            project_path = os.path.join(default_projects_dir_path, project_id)
            os.makedirs(project_path)
            git_username = _determine_git_username()
            remote_url = f'https://github.com/{git_username}/{project_name}'
            proc = subprocess.run(['git', 'clone', remote_url, project_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode:
                print('Error in handle_project_context attempting to clone project from remote (github) repo')
                err_msg = proc.stderr.decode(errors='replace') if proc.stderr else 'git clone failed'
                return err_msg
            #extremely crude check for valid ipv4 format, just ensures there are 4 . separated sections (i.e. there are 3 .s)
            if len(client_ip.split('.')) == 4:
                add_project_to_list(project_name, project_path, client_ip=client_ip, set_as_current_project=True)
        else:
            print(project_path)

    if not current_project:
        return 'Failed to set current project context'
    project_root = current_project.get('project_root_path', '')
    if not project_root:
        return 'Current project is missing project_root_path'
    cwd = project_root

    #this line also updates the project within projects_dict, since current_project was assigned from it as a reference
    current_project['last_command_timestamp'] = now
    save_projects_dict()
    return None

def scan_for_git_repos(scan_root_dir:str, project_names: list[str]|None=None, ignore_subrepos=True) -> list[str]:
    '''Recursively scan scan_root_dir and all subdirs for GIT projects'''
    if project_names is None:
        project_names = []
    if not os.path.exists(scan_root_dir):
        raise FileNotFoundError(f'Error, cannot scan in nonexistant directory path: {scan_root_dir}')
    elif os.path.isfile(scan_root_dir):
        raise RuntimeError(f'Error: given directory path is actually a file: {scan_root_dir}')
    found_git_projects = []
    for dirpath, dirnames, filenames in os.walk(scan_root_dir):
        if '.git' in dirnames:
            if not project_names:
                found_git_projects.append(dirpath)
            else:
                name = os.path.split(dirpath)[1]
                if name in project_names:
                    found_git_projects.append(dirpath)
            if ignore_subrepos:
                dirnames[:] = []
    return found_git_projects

def _ensure_known_git_repos_file() -> None:
    global known_git_repos
    known_git_repos_path = os.path.join(server_dir_path, known_git_repos_filename)
    #if known_git_repos_path already exists, attempt to load from it.  Hard-fail on failure.
    if os.path.exists(known_git_repos_path):
        try:
            with open(known_git_repos_path, 'r') as f:
                payload = json.load(f)
        except json.JSONDecodeError as e:
            raise RuntimeError(f'Error decoding JSON in known git repos file: {known_git_repos_path}') from e
        except OSError as e:
            raise RuntimeError(f'Error opening known git repos file: {known_git_repos_path}') from e

        if not isinstance(payload, dict):
            raise RuntimeError(f'Invalid known git repos payload type at {known_git_repos_path}: expected object')
        loaded_known_git_repos = payload.get('known_git_repos', [])
        if not isinstance(loaded_known_git_repos, list):
            raise RuntimeError(f'Invalid known_git_repos value at {known_git_repos_path}: expected list')

        for path in loaded_known_git_repos:
            if isinstance(path, str) and path and path not in known_git_repos:
                known_git_repos.append(path)
        return
    #get command line input from the user
    user_arg = input('Enter root path to scan for git repos, or provide a filepath with a newline separated list of paths')
    user_arg = os.path.expanduser(user_arg)
    paths_to_scan = []
    found_repos = []
    if not os.path.exists(user_arg):
        raise FileNotFoundError(f'Cannot find file or directory at path: {user_arg}')
    elif os.path.isfile(user_arg):
        with open(user_arg, 'r') as f:
            paths_to_scan = [line.strip() for line in f.readlines() if line.strip()]
            #ensure all paths in paths_to_scan are absolute paths
            paths_to_scan = [path if os.path.isabs(path) else os.path.abspath(path) for path in paths_to_scan]
    else:
        #user_arg is a directory, so just add it to paths_to_scan as the only path to scan
        path_to_scan = user_arg if os.path.isabs(user_arg) else os.path.abspath(user_arg)
        paths_to_scan.append(path_to_scan)

    for path in paths_to_scan:
        found_repos.extend(scan_for_git_repos(path))

    for path in found_repos:
        if not path in known_git_repos:
            known_git_repos.append(path)

    #ensure parent folder of known_git_repos_path exists
    ensure_directory_exists(server_dir_path)
    with open(known_git_repos_path, 'w') as f:
        try:
            json.dump({'known_git_repos': known_git_repos}, f)
        except Exception as e:
            print(f'Error trying to write json to known_git_repos file: {e}')



def initialize():
    global project_runtime_dir_path, projects_dict, current_project, cwd
    #actual literal cwd that rxs is running from
    literal_cwd = os.getcwd()
    current_project = {}
    _ensure_projects_dict_file()
    _ensure_default_projects_dir()
    _ensure_known_git_repos_file()
    projects_dict = load_projects_dict()
    if projects_dict:
        most_recent_time = 0.0
        most_recent_project_id = list(projects_dict.keys())[0]
        for proj_id, proj in projects_dict.items():
            timestamp = proj.get('last_command_timestamp', 0.0)
            if timestamp > most_recent_time:
                most_recent_project_id = proj_id
                most_recent_time = timestamp

        set_current_project(project_id=most_recent_project_id)
        current_project = projects_dict[most_recent_project_id]

    current_proj_path = current_project.get('project_root_path', '')
    if current_proj_path:
        cwd = current_proj_path

    project_runtime_dir_path = os.path.join(cwd, project_runtime_dir_name)

    #if the server program is launched from inside of a known git repo, until proven otherwise, we will treat
    #that repo as the current rxs project
    if is_git_repo(literal_cwd):
        cwd_project_name = os.path.split(literal_cwd)[1]
        known_repo_names = [os.path.split(path)[1] for path in known_git_repos]
        if cwd_project_name in known_repo_names:
            set_current_project(project_name=cwd_project_name)




