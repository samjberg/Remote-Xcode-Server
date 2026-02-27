import os, sys, pathlib, socket, subprocess
from typing import BinaryIO
from mimetypes import guess_type

server_port = 8751
socket_port = 50682
runtime_dir_name = '.remote-xcode-server'

def get_server_port() -> int:
    return server_port

def get_runtime_dir_name() -> str:
    return runtime_dir_name

def get_runtime_dir_path(cwd:str|None=None) -> str:
    """Returns the directory Remote-Xcode-Server uses to save files on both client and server"""
    if cwd is None:
        cwd = os.getcwd()
    return os.path.join(cwd, runtime_dir_name)


def unix_path(path:str) -> str:
    """Returns a POSIX compliant version of path"""
    return pathlib.Path(path).as_posix()

def allowed_filename(filename:str) -> bool:
    valid_extensions = ['diff', 'txt']
    return filename.rsplit('.', 1)[-1].lower() in valid_extensions

def get_build_log_path(job_id:int) -> str:
    cwd = os.getcwd()
    runtime_path = get_runtime_dir_path(cwd)
    filename = f'buildlog-{job_id}.txt'
    build_log_path = os.path.join(runtime_path, filename)
    return build_log_path


def uploads_folder_exists() -> bool:
    cwd = os.getcwd()
    if runtime_dir_name not in os.listdir(cwd):
        return False
    return os.path.isdir(get_runtime_dir_path(cwd))

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
        for name in os.listdir(current_path):
            if name.split('.')[-1] == 'xcodeproj': #if the file extension is 'xcodeproj'
                return current_path
        path_parts = path_parts[:-1]
    return cwd

def get_appname() -> str:
    root_path = get_project_root_path()
    for name in os.listdir(root_path):
        name_parts = name.split('.')
        if name_parts[-1] == 'xcodeproj':
            return name_parts[0]#.replace(' ', '_')
    return root_path.split('/')[-1]#.replace(' ', '_')


def update_gitignore():
    start_dir = os.getcwd()
    project_root = get_project_root_path(start_dir)
    gitignore_path = os.path.join(project_root, '.gitignore')
    ignores_runtime_dir = False
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, 'w') as f:
            f.write(f'/{runtime_dir_name}/\n')
    else:
        ends_with_newline = False
        with open(gitignore_path, 'r') as f:
            lines = f.readlines()

        for line in lines:
            for s in [f'/{runtime_dir_name}/', f'{runtime_dir_name}/']:
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
            additions.append(f'/{runtime_dir_name}/\n')

        if additions:
            with open(gitignore_path, 'a') as f:
                if lines and not ends_with_newline:
                    f.write('\n')
                f.writelines(additions)


def is_plaintext(path:str):
    """Return True if the file at the supplied path is a plaintext, otherwise returns False"""
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


def is_subdir(path, directory):
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)

    relative = os.path.relpath(path, directory)

    if relative.startswith(os.pardir):
        return False
    else:
        return True


def get_changed_file_paths(scope='repo') -> list[str]:
    '''
    Returns a list of paths to all files that have been changed relative to HEAD, including untracked files.

    [scope] is 'repo' by default, which will detect all changes in the entire repo.  If 'cwd' is passed instead,

    only changes in the current directory (and subdirectories) will be included.'''
    # Use argv form (no shell) for cross-platform behavior.
    # Exclude .gitignore in Python instead of relying on shell/pathspec parsing.
    cwd = os.getcwd()
    project_root_path = get_project_root_path()
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
        print(f'No output from {' '.join(diff_command)} in proc.stdout')

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
        print(f'No output from {' '.join(untracked_diff_command)} in proc.stdout')

    untracked_lines_bytes = [b for b in untracked_proc.stdout.split(b'\x00') if b]
    untracked_file_paths = [b.decode(errors='replace') for b in untracked_lines_bytes]
    untracked_file_paths = [p for p in untracked_file_paths if p and p != '.gitignore']

    for path in untracked_file_paths:
        if not path in file_paths:
            file_paths.append(path)

    return file_paths




def prepare_text_changes() -> tuple[str, list[str]]:
    cwd = unix_path(os.getcwd())
    diffs_path = get_runtime_dir_path(cwd)
    diff_filename = 'gitdiff.diff'
    git_diff_path = os.path.join(diffs_path, diff_filename)
    if os.path.exists(git_diff_path):
        os.remove(git_diff_path)
    os.system('git add .')

    changed_file_paths = [path for path in get_changed_file_paths() if path]
    changed_binary_paths = [path for path in changed_file_paths if not is_plaintext(path.split('/')[-1])]
    changed_text_paths = [path for path in changed_file_paths if path not in changed_binary_paths and path != '.gitignore']

    
    # Build a patch that only contains plaintext files; binary files are sent separately.
    with open(git_diff_path, 'w', newline='') as diff_file:
        if changed_text_paths:
            subprocess.run(['git', 'diff', 'HEAD', '--', *changed_text_paths], stdout=diff_file)

    return git_diff_path, changed_binary_paths


def send_bytes(b:bytes, conn:socket.socket, chunk_size:int=4096, start_pos=0):
    msg_size = len(b) - start_pos
    if msg_size <= chunk_size:
        conn.sendall(b[start_pos:])
    else: #b is a bytestring longer than chunk_size
        pos = min(start_pos, msg_size-1)
        while pos < msg_size:
            pos += conn.send(b[pos:pos+chunk_size])
        


def send_file_contents(f:BinaryIO, conn:socket.socket, chunk_size:int=4096):
    while True:
        chunk = f.read(chunk_size)
        if not chunk:
            break
        conn.sendall(chunk)



def recv_file_contents(s:socket.socket, chunk_size:int=4096, dest_file=None, print_output=True):
    while True:
        new_bytes = s.recv(chunk_size)
        if not new_bytes:
            break
        new_text = new_bytes.decode('utf-8')
        if print_output:
            print(new_text)
        if dest_file:
            pass

        





