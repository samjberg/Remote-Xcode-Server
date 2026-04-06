import hashlib
import os, sys, pathlib, socket, subprocess, shlex, datetime, shutil
from typing import Callable, Optional, TypeVar, Union
from mimetypes import guess_type
from uuid import uuid4
from flask import Response
from requests.models import DecodeError

try:
    from typing import ParamSpec
except ImportError:
    from typing_extensions import ParamSpec

#who even knows what these two lines are.  But they are necessary to enable typehinting for functions (i.e. run_process)
#that use the decorator @handle_process_errors
P = ParamSpec("P")
R = TypeVar("R", bound=subprocess.CompletedProcess)
KB = 1024
MB = KB * KB
project_runtime_dir_name = '.remote-xcode-server'
server_dir_name = '.remote-xcode-server'
project_nonspecific_routes = [
        '/enable_pairing',
        '/pairing-bootstrap',
        '/discovery-status',
        '/add_allowed_interactive_command',
        '/remove_allowed_interactive_command',
        '/get_allowed_interactive_commands',
        '/checkprogress/',
        '/status/',
    ]

def route_is_project_specific(route: str) -> bool:
    return not any([route.startswith(pathroute) for pathroute in project_nonspecific_routes])

def ensure_directory_exists(dir_path: str) -> None:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)
    elif not os.path.isdir(dir_path):
        #dir_path exists but is a file.  Remove it and then run makedirs
        os.remove(dir_path)
        os.makedirs(dir_path)

def is_git_repo(path:str) -> bool:
    if '.git' in os.listdir(path):
        return os.path.isdir(os.path.join(path, '.git'))
    return False

def get_project_runtime_dir_name() -> str:
    return project_runtime_dir_name

def get_server_dir_name() -> str:
    return server_dir_name

def get_project_runtime_dir_path(cwd:Optional[str]=None) -> str:
    """Returns the directory Remote-Xcode-Server uses to save files on both client and server"""
    if cwd is None:
        cwd = os.getcwd()
    return os.path.join(cwd, project_runtime_dir_name)

def get_server_dir_path() -> str:
    return os.path.join(get_user_home_dir(), server_dir_name)

# Backward-compatible names used throughout the existing codebase.
def get_runtime_dir_name() -> str:
    return get_project_runtime_dir_name()

def get_user_runtime_dir_name() -> str:
    return get_server_dir_name()

def get_runtime_dir_path(cwd:Optional[str]=None) -> str:
    return get_project_runtime_dir_path(cwd)

def get_user_runtime_dir_path() -> str:
    return get_server_dir_path()

def unix_path(path:str) -> str:
    """Returns a POSIX compliant version of path"""
    return pathlib.Path(path).as_posix()

def allowed_filename(filename:str) -> bool:
    valid_extensions = ['diff', 'txt']
    return filename.rsplit('.', 1)[-1].lower() in valid_extensions

def get_build_log_path(job_id:int) -> str:
    cwd = os.getcwd()
    runtime_path = get_project_runtime_dir_path(cwd)
    filename = f'buildlog-{job_id}.txt'
    build_log_path = os.path.join(runtime_path, filename)
    return build_log_path


def get_bundle_name(project_id: str):
    return f'{project_id}.bundle'

def mailbox_has_bundle_request(mailbox:dict, project_id:str='', project_name:str='', bundle_request:dict|None=None) -> bool:
    if not mailbox.get('bundle_requests', None):
        return False
    if bundle_request:
        if not project_id:
            project_id = bundle_request.get('id', '')
        if not project_name:
            project_name = bundle_request.get('project_name', '')

    if (not project_id) and (not project_name):
        raise RuntimeError('Must provide project_id, project_name, or bundle_request')

    if not project_id:
        project_id = generate_project_id(project_name)

    for br in mailbox.get('bundle_requests', []):
        if (br.get('id', '') == project_id) or (br.get('project_name') == project_name):
            return True

    return False



def mailbox_get_bundle_request(mailbox:dict, project_id:str='', project_name:str='', bundle_request:dict|None=None) -> dict:
    if not mailbox.get('bundle_requests', None):
        return {}
    if bundle_request:
        if not project_id:
            project_id = bundle_request.get('id', '')
        if not project_name:
            project_name = bundle_request.get('project_name', '')

    if (not project_id) and (not project_name):
        raise RuntimeError('Must provide project_id, project_name, or bundle_request')

    if not project_id:
        project_id = generate_project_id(project_name)

    for br in mailbox.get('bundle_requests', []):
        if (br.get('id', '') == project_id) or (br.get('project_name') == project_name):
            return br

    return {}

def mailbox_remove_bundle_request(mailbox: dict, project_id: str) -> dict:
    if not isinstance(mailbox.get('bundle_requests', False), list):
        return mailbox
    new_bundle_requests = []
    for req in mailbox.get('bundle_requests', []):
        if req.get('id', '') != project_id:
            new_bundle_requests.append(req)
    mailbox['bundle_requests'] = new_bundle_requests
    return mailbox


def content_type(resp: Response):
    return resp.headers.get('Content-Type', '')


def http_status_code_is_ok(status_code: int):
    if not isinstance(status_code, int):
        raise TypeError('Error: invalid type {type(status_code)} for status_code')
    return str(status_code).startswith('2')



def uploads_folder_exists(project_id: str = '', project_name: str = '') -> bool:
    '''Checks for the existence of the uploads folder.  This is a per-project folder, so this is a per-project function'''
    # Local import avoids circular import at module load time:
    # mcp_utils -> projects_context_manager -> mcp_utils
    from projects_context_manager import get_project
    if project_id:
        project = get_project(project_id=project_id)
    elif project_name:
        project = get_project(project_name=project_name)
    else:
        raise RuntimeError('Must provide either project_id or project_name as argument to upload_folder_exists')

    cwd = project.get('project_root_path', '')
    if project_runtime_dir_name not in os.listdir(cwd):
        return False
    return os.path.isdir(get_project_runtime_dir_path(cwd))

def get_project_name() -> str:
    dir_contents = os.listdir()
    for name in dir_contents:
        name_parts = name.split('.')
        if name_parts[-1] == 'xcodeproj':
            return name_parts[0]
    return os.getcwd().split('/')[-1]

def get_project_root_path(cwd:str='.') -> str:
    if cwd == '.':
        cwd = os.getcwd()
    if '\\' in cwd:
        cwd = unix_path(cwd)
    if cwd.lower() in ['/', 'c:/']:
        print('Come on dude, seriously?  Don\'t pass in the filesystem root.')
    path_parts = cwd.split('/')
    while len(path_parts) > 1:
        current_path = '/'.join(path_parts)
        if '.git' in os.listdir(current_path):
            return current_path
        path_parts = path_parts[:-1]
    raise RuntimeError(f'Unable to find project root path from given cwd: {cwd}')

def get_user_home_dir() -> str:
    home_dir_unsanitized = os.path.expanduser('~')
    home_dir_path = unix_path(home_dir_unsanitized)
    return home_dir_path



def get_appname(cwd:str='') -> str:
    if not cwd:
        cwd = os.getcwd()
    root_path = get_project_root_path(cwd)
    return os.path.split(root_path)[-1]
    # return root_path.split('/')[-1]#.replace(' ', '_')

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0)
    try:
        # random IP doesn't even have to be reachable
        s.connect(('10.254.254.254', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def contains_any(text:str, candidates:list[str], case_sensitive:bool=True) -> bool:
    '''Returns whether or not [text] contains any of the strings in [candidates]'''
    if case_sensitive:
        for candidate in candidates:
            if candidate in text:
                return True
    else:
        text = text.lower()
        for candidate in candidates:
            if candidate.lower() in text:
                return True
    return False


def clear_directory(path) -> None:
    for item_path in os.listdir(path):
        os.remove(os.path.join(path, item_path))

def dir_is_empty(path) -> bool:
    return len(os.listdir(path)) == 0

def update_gitignore(project_root: str):
    if not project_root:
        return
    gitignore_path = os.path.join(project_root, '.gitignore')
    ignores_runtime_dir = False
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, 'w') as f:
            f.write(f'/{project_runtime_dir_name}/\n')
    else:
        ends_with_newline = False
        with open(gitignore_path, 'r') as f:
            lines = f.readlines()

        for line in lines:
            for s in [f'/{project_runtime_dir_name}/', f'{project_runtime_dir_name}/']:
                if s == line or s in line:
                    ignores_runtime_dir = True

        if lines:
            # Check the actual on-disk trailing byte so newline translation doesn't
            # affect the result on Windows.
            with open(gitignore_path, 'rb') as f:
                f.seek(-1, os.SEEK_END)
                ends_with_newline = f.read(1) in (b'\n', b'\r')

        additions = []
        if not ignores_runtime_dir:
            additions.append(f'/{project_runtime_dir_name}/\n')

        if additions:
            with open(gitignore_path, 'a') as f:
                if lines and not ends_with_newline:
                    f.write('\n')
                f.writelines(additions)


def apply_patch(patch_path:str) -> None:
    '''Applies the git patch located at patch_path'''
    command = f'git apply {patch_path}'
    os.system(command)



def is_plaintext(path:str):
    '''Return True if the file at the supplied path is a plaintext, otherwise returns False'''
    path = unix_path(path)
    name = path.split('/')[-1]

    ext = name.rsplit('.', 1)[-1].lower()
    #plaintext file extensions that are not recognized by mimetype.guess_type() and will return (None, None) and so must be handled manually
    csv_path = os.path.join(sys.path[0], 'plaintext_extensions.csv')
    with open(csv_path, 'r') as f:
        plaintext_extensions = f.read().split(',')
    if ext in plaintext_extensions:
        return True
    guess = guess_type(path, strict=False)[0]
    if guess and type(guess) == str:
        return guess[:4] == 'text'
    return False

def split_paths_by_text_or_binary(paths:list[str]) -> tuple[list[str], list[str]]:
    '''Splits paths into two lists [plaintext_paths, binary_paths] based on the type of the files located at the paths'''
    plaintext_paths:list[str] = [path for path in paths if is_plaintext(path)]
    binary_paths = [path for path in paths if not path in plaintext_paths]
    return plaintext_paths, binary_paths


def is_subdir(path, directory):
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)

    relative = os.path.relpath(path, directory)

    if relative.startswith(os.pardir):
        return False
    else:
        return True

def normalize_file_line_endings(path:str, fmt='LF') -> None:
    #guard against invalid fmt option. This makes it valid to just use if/else instead of if/elif in the for loop
    if fmt.upper() not in ['LF', 'CRLF']:
        print(f'Invalid line endings format: {fmt}')
        return

    #guard against invalid path
    if not os.path.exists(path):
        raise FileNotFoundError

    with open(path, 'rb') as f:
        text_bytes = f.read()

    lines_bytes = text_bytes.splitlines(keepends=False)
    newline_bytes = b'\n' if fmt == 'LF' else b'\r\n'

    #write over the file with normalized line endings
    with open(path, 'wb') as f:
        for line in lines_bytes:
            clean_line = line.rstrip()
            f.write(clean_line + newline_bytes)


def _normalize_path_for_compare(path: str) -> str:
    if path is None:
        return ''
    normalized = os.path.normpath(os.path.expandvars(os.path.expanduser(path.strip())))
    if os.name == 'nt':
        return unix_path(os.path.normcase(normalized))
    return normalized #any "Code is structurally unreachable" warning on this line is caused by Pylance doing static analysis and assuming it will always be run
                      #on the current OS.  It is not an actual bug or anything to worry about.


def generate_project_id(project_name: str) -> str:
    if not project_name:
        return ''
    normed_project_name = _normalize_path_for_compare(project_name)
    #take 16 (hex) digits from the center of sha256 hexdigest
    s = hashlib.sha256(normed_project_name.encode()).hexdigest()[24:40]
    #format into project_id format
    project_id = '-'.join([s[:6], s[6:10], s[10:16]])
    return project_id

def get_git_username(project_root_path='') -> str:
    project_root_path = project_root_path if project_root_path else get_project_root_path(os.getcwd())
    proc = run_process(['git', 'config', '--list'], cwd=project_root_path)
    if proc.returncode:
        err_msg = proc.stderr.decode(errors='replace')
        raise RuntimeError(f'Error running git config --list to find git username.  Error message: {err_msg}')
    
    text = ''
    try:
        text = proc.stdout.decode(errors='replace')

    except Exception as e:
        raise RuntimeError(f'Error decoding output og git config --list to find git username.  Error message: {e}')

    if not text:
        raise RuntimeError('Error, no output from git config --list (to find git username)')

    username = ''
    lines: list[str] = text.splitlines()
    for line in lines:
         #just to be safe there is no space or whatever on the username
        line_parts = [part.strip() for part in line.split('=')]
        if line_parts[0] == 'user.name' and len(line_parts) >= 2:
            username = line_parts[1] 
            break
        else:
            raise ValueError('Apparently malformed output line from git config --list.\nOffending line: {line}')

    return username








#this insane P and R stuff with ParamSpec and TypeVar is just the insanity that is necessary to make typehints show up for functions
#that use this decorator (i.e. run_process) for some insane reason.
import subprocess
def handle_process_errors(f: Callable[P, R]) -> Callable[P, R]:
    '''Decorator which takes in a function that runs and returns the result of subprocess.run, and handles error handling for it'''
    def inner(*args, **kwargs):
        proc = f(*args, **kwargs)
        if proc.returncode != 0:
            err_text = proc.stderr.decode(errors='replace') if proc.stderr else ''
            command = proc.args if isinstance(proc.args, str) else ' '.join(map(str, proc.args))
            print(f'Running command: {command} returned non-zero return code: {proc.returncode}')
            print(f'Error message: {err_text}')
            raise subprocess.CalledProcessError(
                proc.returncode,
                proc.args,
                output=proc.stdout,
                stderr=proc.stderr,
            )
        return proc
    return inner


@handle_process_errors
def run_process(command:list[str], stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd:str=None) -> subprocess.CompletedProcess:
    proc = subprocess.run(command, stdout=stdout, stderr=stderr, cwd=cwd)
    return proc

def get_commit_date(branch:str) -> Optional[datetime.datetime]:
    command = f'git show {branch}'.split(' ')
    proc = run_process(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not proc.stdout:
        print(f"No output from command: {' '.join(proc.args)}")
        return None
    text:str = proc.stdout.decode(errors='replace')
    lines = text.splitlines()
    date_part_str = None
    for line in lines:
        if line.startswith('Date'):
            date_part_str = line.split()
            break
    if not date_part_str:
        print(f"Date line not found in output from {' '.join(proc.args)}")
        return None
    date_part_str = date_part_str[1:]
    months = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
    month = months.index(date_part_str[1].lower()) + 1
    day = int(date_part_str[2])
    hour, minute, second = [int(x.strip()) for x in date_part_str[3].split(':')]
    year = int(date_part_str[4])
    creation_dt = datetime.datetime(year=year, month=month, day=day, hour=hour, minute=minute, second=second)
    return creation_dt


def get_commits_between(start_ref:str, end_ref:str='HEAD') -> list[str]:
    '''Returns a list of all commits "between" start_ref and end_ref, inclusive'''
    cmd = shlex.split(f'git rev-list {start_ref} ^{end_ref}')
    proc:subprocess.CompletedProcess = _run_git_capture(cmd)
    proc.check_returncode()
    try:
        res_str:str = proc.stdout.decode(errors='replace')
    except UnicodeDecodeError as err:
        print(f'Failed to decode output for command: {cmd}\nError message: {err}')
        return []
    commits = [line.strip() for line in res_str.splitlines()]
    return commits

def git_create_full_project_bundle(project_root_path:str, save_path:str = ''):
    '''Creates a "full project bundle" (all refs) of the git repo rooted at project_root_path'''
    project_name = get_appname(project_root_path)
    bundle_name = f'{project_name}.bundle'
    proc = run_process(shlex.split(f'git bundle create {bundle_name} --all'), cwd=project_root_path)
    if proc.returncode:
        raise RuntimeError('Error creating full project git bundle for project {project_name} at path: {project_root_path}')
    bundle_path = os.path.join(project_root_path, bundle_name)
    if save_path:
        shutil.copy2(bundle_path, save_path)
        os.remove(bundle_path)
        return save_path
    return bundle_path



def git_create_update_bundle(start_ref:str, end_ref:str='HEAD', save_path='update.bundle') -> str:
    '''Creates an incremental .bundle, which will bring all commits missing (reachable) from start_ref but ARE present in (reachable from) end_ref'''
    cmd = shlex.split(f'git bundle create {save_path} {start_ref}..{end_ref} --all')
    proc = _run_git_capture(cmd)
    proc.check_returncode()
    try:
        res = proc.stdout.decode(errors='replace')
    except UnicodeDecodeError as err:
        print(f"Error decoding output from command: {' '.join(cmd)}\nError Message: {err}")
    return save_path

def git_verify_bundle(bundle_path:str) -> bool:
    '''Verifies that the git bundle is both a valid bundle in general, and also can be successfully applied to the current repo without errors'''
    project_root = get_project_root_path()
    if not is_subdir(bundle_path, project_root):
        raise FileNotFoundError('')
    verify_bundle_command = shlex.split(f'git bundle verify {bundle_path}')
    proc:subprocess.CompletedProcess = _run_git_capture(verify_bundle_command)
    proc.check_returncode()
    if not proc:
        print(f'git bundle verify {bundle_path} produced no output')
    try:
        res:str = proc.stdout.decode(errors='replace')
    except UnicodeDecodeError as err:
        print(f"Error decoding output from command {' '.join(verify_bundle_command)}\nError Message: {err}")
        return False

    lines = res.splitlines()
    for line in lines:
        if line.startswith('error'):
            return False
    return True



def git_apply_update_bundle(bundle_path:str) -> bool:
    '''Applies an update bundle located at bundle_path to the current directory.  All branches/refs will be updated, the entire repo should become identical to the source'''
    valid_bundle = git_verify_bundle(bundle_path)
    if not valid_bundle:
        print('Error: bundle was inv')
        return False
    heads_part = '+refs/heads/*:refs/heads/*'
    tags_part = '+refs/tags/*:refs/tags/*'
    command_str = f"git fetch {bundle_path} '{heads_part}' '{tags_part}'"
    command = shlex.split(command_str)
    proc = _run_git_capture(command)
    proc.check_returncode()
    return True



def get_git_branches(cwd:str, app_name:str='', return_current_branch=False, sort_order='creatordate') -> Union[list[str], tuple[list[str], str]]:
    if not app_name:
        app_name = get_appname(cwd)
    git_command = f'git branch --sort={sort_order}'.split(' ')
    proc = run_process(git_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    if not proc.stdout:
        print(f"No output from {' '.join(git_command)}")

    text: str = proc.stdout.decode()
    lines = text.splitlines()
    #this is just how git branch output is formatted.  All names start on the 3rd (index 2) character.  The current branch has a * as the 0th index
    return_lines = [line[2:].strip() for line in lines]
    if not return_current_branch:
        return return_lines

    current_branch = ''
    for line in lines:
        #current branch line starts with '*'
        if line[0] == '*':
            current_branch = line
            break

    return return_lines, current_branch


def get_merge_base(commit_hash1:str, commit_hash2:str) -> str:
    command = f'git merge-base {commit_hash1} {commit_hash2}'.split(' ')
    project_root = get_project_root_path()
    proc = run_process(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=project_root)
    if not proc.stdout:
        print(f"No output from {' '.join(proc.args)}")
        return ''
    merge_base = proc.stdout.decode(errors='replace').strip()
    return merge_base



def compare_ahead_behind(commit1:str, commit2:str) -> Union[tuple[int, int], None]:
    '''Returns two ints, representing how many commits ahead each commit is compared to the other'''
    command = shlex.split(f'git rev-list --left-right --count {commit1}...{commit2}')
    project_root = get_project_root_path()
    proc = run_process(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=project_root)
    if not proc.stdout:
        print(f"No output from {' '.join(proc.args)}")
        return None
    text = proc.stdout.decode(errors='replace').strip()
    ahead, behind = [int(x) for x in text.split()]
    return ahead, behind


def _run_git_capture(command:list[str], cwd:Optional[str]=None) -> subprocess.CompletedProcess:
    """Run git command and capture stdout/stderr without raising."""
    if cwd is None:
        cwd = get_project_root_path()
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)


def _decode_stdout_stderr(proc:subprocess.CompletedProcess) -> tuple[str, str]:
    stdout_text = proc.stdout.decode(errors='replace').strip() if proc.stdout else ''
    stderr_text = proc.stderr.decode(errors='replace').strip() if proc.stderr else ''
    return stdout_text, stderr_text


def git_has_origin(cwd:Optional[str]=None) -> bool:
    proc = _run_git_capture(['git', 'remote'], cwd=cwd)
    if proc.returncode != 0:
        return False
    text, _ = _decode_stdout_stderr(proc)
    remotes = [line.strip() for line in text.splitlines() if line.strip()]
    return 'origin' in remotes


def git_has_commit(commit_hash:str, cwd:Optional[str]=None) -> bool:
    # git cat-file returns non-zero when object is missing.
    proc = _run_git_capture(['git', 'cat-file', '-e', f'{commit_hash}^{{commit}}'], cwd=cwd)
    return proc.returncode == 0


def git_ahead_behind(left_commit:str, right_commit:str, cwd:Optional[str]=None) -> Optional[tuple[int, int]]:
    proc = _run_git_capture(
        ['git', 'rev-list', '--left-right', '--count', f'{left_commit}...{right_commit}'],
        cwd=cwd,
    )
    if proc.returncode != 0:
        return None

    text, _ = _decode_stdout_stderr(proc)
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        left_ahead = int(parts[0])
        right_ahead = int(parts[1])
    except ValueError:
        return None
    return left_ahead, right_ahead


def git_dirty_tracked(cwd:Optional[str]=None) -> bool:
    if cwd is None:
        cwd = get_project_root_path()

    unstaged = _run_git_capture(['git', 'diff', '--quiet', '--ignore-submodules', '--'], cwd=cwd)
    staged = _run_git_capture(['git', 'diff', '--cached', '--quiet', '--ignore-submodules', '--'], cwd=cwd)
    return unstaged.returncode != 0 or staged.returncode != 0


def git_dirty_untracked_count(cwd:Optional[str]=None) -> int:
    proc = _run_git_capture(['git', 'ls-files', '--others', '--exclude-standard'], cwd=cwd)
    if proc.returncode != 0:
        return 0
    text, _ = _decode_stdout_stderr(proc)
    if not text:
        return 0
    return len([line for line in text.splitlines() if line.strip()])


def get_git_state(cwd:Optional[str]=None) -> dict:
    """
    Returns a dict with the following entries (using format 'name: type'))
        head: str
        current_branch: str
        branches: list[str]
        is_detached: bool
        has_origin: bool
        dirty_tracked: bool
        dirty_untracked_count
    """
    if cwd is None:
        cwd = get_project_root_path()

    head_proc = _run_git_capture(['git', 'rev-parse', 'HEAD'], cwd=cwd)
    if head_proc.returncode != 0:
        _, err_text = _decode_stdout_stderr(head_proc)
        raise RuntimeError(f'Failed to read HEAD commit: {err_text}')
    head, _ = _decode_stdout_stderr(head_proc)

    branch_proc = _run_git_capture(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=cwd)
    if branch_proc.returncode != 0:
        _, err_text = _decode_stdout_stderr(branch_proc)
        raise RuntimeError(f'Failed to read current branch: {err_text}')
    current_branch, _ = _decode_stdout_stderr(branch_proc)
    is_detached = current_branch == 'HEAD'
    if is_detached:
        current_branch = ''

    branches_proc = _run_git_capture(['git', 'branch', '--format=%(refname:short)'], cwd=cwd)
    if branches_proc.returncode != 0:
        _, err_text = _decode_stdout_stderr(branches_proc)
        raise RuntimeError(f'Failed to list local branches: {err_text}')
    branches_text, _ = _decode_stdout_stderr(branches_proc)
    branches = [line.strip() for line in branches_text.splitlines() if line.strip()]

    return {
        'head': head,
        'current_branch': current_branch,
        'branches': branches,
        'is_detached': is_detached,
        'has_origin': git_has_origin(cwd),
        'dirty_tracked': git_dirty_tracked(cwd),
        'dirty_untracked_count': git_dirty_untracked_count(cwd),
    }


def execute_git_action(action:str, args:Optional[dict]=None, cwd:Optional[str]=None) -> dict:
    if args is None:
        args = {}
    if cwd is None:
        cwd = get_project_root_path()

    allowed_arg_keys = {
        'fetch_origin': set(),
        'checkout_branch': {'branch'},
        'checkout_branch_at_commit': {'branch', 'commit'},
        'ff_only_to_commit': {'commit'},
        'has_commit': {'commit'},
        'ahead_behind': {'left', 'right'},
        'backup_remove_gitignore': set(),
        'create_update_bundle': {'path', 'start_ref', 'end_ref'},
        'apply_update_bundle': {'path'}
    }

    if action not in allowed_arg_keys:
        return {
            'success': False,
            'action': action,
            'error': f'Unknown action: {action}',
        }

    supplied_keys = set(args.keys())
    required_keys = allowed_arg_keys[action]
    if supplied_keys != required_keys:
        return {
            'success': False,
            'action': action,
            'error': f'Invalid args for {action}. Required keys: {sorted(required_keys)}, supplied: {sorted(supplied_keys)}',
        }

    if action == 'backup_remove_gitignore':
        gitignore_path = os.path.join(cwd, '.gitignore')
        runtime_dir_path = get_runtime_dir_path(cwd)
        os.makedirs(runtime_dir_path, exist_ok=True)

        if not os.path.exists(gitignore_path):
            return {
                'success': True,
                'action': action,
                'command': ['file-op', 'backup_remove_gitignore'],
                'returncode': 0,
                'stdout': '.gitignore not found; nothing to remove.',
                'stderr': '',
                'removed': False,
                'backup_path': '',
            }

        timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_filename = f'gitignore-backup-{timestamp}.gitignore'
        backup_path = os.path.join(runtime_dir_path, backup_filename)
        counter = 1
        while os.path.exists(backup_path):
            backup_filename = f'gitignore-backup-{timestamp}-{counter}.gitignore'
            backup_path = os.path.join(runtime_dir_path, backup_filename)
            counter += 1

        shutil.copy2(gitignore_path, backup_path)
        os.remove(gitignore_path)

        return {
            'success': True,
            'action': action,
            'command': ['file-op', 'backup_remove_gitignore'],
            'returncode': 0,
            'stdout': f'Backed up and removed .gitignore: {unix_path(backup_path)}',
            'stderr': '',
            'removed': True,
            'backup_path': unix_path(backup_path),
        }

    if action == 'fetch_origin':
        command = ['git', 'fetch', 'origin']
    elif action == 'checkout_branch':
        command = ['git', 'checkout', args['branch']]
    elif action == 'checkout_branch_at_commit':
        command = ['git', 'checkout', '-B', args['branch'], args['commit']]
    elif action == 'ff_only_to_commit':
        command = ['git', 'merge', '--ff-only', args['commit']]
    elif action == 'has_commit':
        command = ['git', 'cat-file', '-e', f"{args['commit']}^{{commit}}"]
    elif action == 'ahead_behind':
        command = ['git', 'rev-list', '--left-right', '--count', f"{args['left']}...{args['right']}"]
    elif action == 'create_update_bundle':
        command = ['git', 'bundle', 'create', args['path'], f"{args['start_ref']}..{args['end_ref']}", '--all']
    elif action == 'apply_update_bundle':
        bundle_path = args['path']
        # Treat relative bundle paths as relative to the selected repo cwd, not process cwd.
        if not os.path.isabs(bundle_path):
            bundle_path = os.path.join(cwd, bundle_path)
        bundle_path = os.path.realpath(bundle_path)
        repo_root = os.path.realpath(cwd)

        if is_subdir(bundle_path, repo_root):
            command = ['git', 'fetch', bundle_path, '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*']
        else:
            raise PermissionError('path argument must be inside of repo')
    else:
        # Defensive fallback even though action is validated above.
        return {'success': False, 'action': action, 'error': f'Unhandled action: {action}'}

    proc = _run_git_capture(command, cwd=cwd)
    stdout_text, stderr_text = _decode_stdout_stderr(proc)
    response = {
        'success': proc.returncode == 0,
        'action': action,
        'command': command,
        'returncode': proc.returncode,
        'stdout': stdout_text,
        'stderr': stderr_text,
    }

    if action == 'has_commit':
        # Missing commit is expected as a state check, not a hard command failure.
        response['has_commit'] = proc.returncode == 0
        response['success'] = True
    elif action == 'ahead_behind' and proc.returncode == 0:
        parts = stdout_text.split()
        if len(parts) == 2:
            try:
                response['left_ahead'] = int(parts[0])
                response['right_ahead'] = int(parts[1])
            except ValueError:
                response['success'] = False
                response['error'] = f'Unable to parse ahead/behind output: {stdout_text}'
        else:
            response['success'] = False
            response['error'] = f'Unexpected ahead/behind output: {stdout_text}'

    return response




def get_current_commit_hash(cwd:str, project_name:str='') -> str:
    if not project_name:
        project_name = get_appname(cwd)
    git_command = 'git rev-parse HEAD'.split(' ')
    proc = subprocess.run(git_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    if proc.returncode != 0:
        err_text = proc.stderr.decode(errors='replace') if proc.stderr else ''
        print(f"Getting current git hash failed with command: {' '.join(git_command)}")
        if err_text:
            print(f'Error message: {err_text}')

    if not proc.stdout:
        print(f"No output from {' '.join(git_command)}")
        raise ValueError(err_text)

    text:str = proc.stdout.decode(errors='replace')
    commit_hash = text.strip()
    return commit_hash





def get_changed_file_paths(cwd:str, scope='repo') -> list[str]:
    '''
    Returns a list of paths to all files that have been changed relative to HEAD, including untracked files.

    [scope] is 'repo' by default, which will detect all changes in the entire repo.  If 'cwd' is passed instead,

    only changes in the current directory (and subdirectories) will be included.'''
    # Use argv form (no shell) for cross-platform behavior.
    # Exclude .gitignore in Python instead of relying on shell/pathspec parsing.
    # project_root_path = get_project_root_path(cwd)
    project_root_path = cwd
    diff_command = ['git', 'diff', '--name-only', '-z', 'HEAD']

    if scope == 'cwd':
        diff_command.extend(['--', '.'])
        diff_run_path = cwd
    else:
        diff_run_path = project_root_path

    proc = subprocess.run(diff_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=diff_run_path)
    if proc.returncode != 0:
        err_text = proc.stderr.decode(errors='replace') if proc.stderr else ''
        print(f'git diff command failed: {" ".join(diff_command)}')
        if err_text:
            print(f'Error message: {err_text}')

    if not proc.stdout:
        print(f"No output from {' '.join(diff_command)} in proc.stdout")

    #split "lines" (file paths) on null byte
    lines_bytes = [b for b in proc.stdout.split(b'\x00') if b]
    file_paths = [b.decode(errors='replace') for b in lines_bytes]
    file_paths = [p for p in file_paths if p and p != '.gitignore']

    untracked_diff_command = ['git', 'ls-files', '--others', '--exclude-standard', '-z']
    untracked_proc = subprocess.run(untracked_diff_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=diff_run_path)

    if untracked_proc.returncode != 0:
        err_text = untracked_proc.stderr.decode(errors='replace') if untracked_proc.stderr else ''
        print(f'git diff command failed: {" ".join(untracked_diff_command)}')
        if err_text:
            print(f'Error message: {err_text}')

    if not untracked_proc.stdout:
        print(f"No output from {' '.join(untracked_diff_command)} in proc.stdout")

    untracked_lines_bytes = [b for b in untracked_proc.stdout.split(b'\x00') if b]
    untracked_file_paths = [b.decode(errors='replace') for b in untracked_lines_bytes]
    untracked_file_paths = [p for p in untracked_file_paths if p and p != '.gitignore']

    for path in untracked_file_paths:
        if not path in file_paths:
            file_paths.append(path)

    return file_paths

def get_diff_for_files(cwd: str, paths:list[str], diff_filename='specific_files_gitdiff.diff') -> str:
    runtime_dir_path = get_runtime_dir_path(cwd)
    git_diff_path = unix_path(os.path.join(runtime_dir_path, diff_filename))
    if os.path.exists(git_diff_path): #my brain would actually explode if this returned true lmao
        os.remove(git_diff_path)

    if not paths:
        #create the file anyway, just put a newline.  It should be read and parsed as having no diff output, which is correct
        with open(git_diff_path, 'w') as diff_file:
            diff_file.write('\n')
        return git_diff_path

    run_process(['git', 'add', '.'], cwd=cwd)
    with open(git_diff_path, 'w') as diff_file:
        run_process(['git', 'diff', 'HEAD', '--', *paths], stdout=diff_file, stderr=subprocess.STDOUT, cwd=cwd)
    return git_diff_path



def prepare_text_changes(cwd:str) -> tuple[str, list[str]]:
    diffs_path = get_runtime_dir_path(cwd)
    diff_filename = 'gitdiff.diff'
    git_diff_path = os.path.join(diffs_path, diff_filename)
    if os.path.exists(git_diff_path):
        os.remove(git_diff_path)
    run_process(['git', 'add', '.'], cwd=cwd)

    changed_file_paths = [path for path in get_changed_file_paths(cwd) if path]
    changed_binary_paths = [path for path in changed_file_paths if not is_plaintext(path.split('/')[-1])]
    changed_text_paths = [path for path in changed_file_paths if path not in changed_binary_paths and path != '.gitignore']


    # Build a patch that only contains plaintext files; binary files are sent separately.
    with open(git_diff_path, 'w', newline='') as diff_file:
        if changed_text_paths:
            subprocess.run(['git', 'diff', 'HEAD', '--', *changed_text_paths], stdout=diff_file, cwd=cwd)

    return git_diff_path, changed_binary_paths
