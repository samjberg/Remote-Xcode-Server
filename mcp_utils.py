import os, sys, pathlib, socket, subprocess
from time import sleep
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
    if cwd is None:
        cwd = os.getcwd()
    return os.path.join(cwd, runtime_dir_name)

def standardize_project_name(name:str) -> str:
    name.replace()

def unix_path(path:str) -> str:
    return pathlib.Path(path).as_posix()

def allowed_filename(filename:str) -> bool:
    valid_extensions = ['diff', 'txt']
    return filename.rsplit('.', 1)[-1].lower() in valid_extensions

def get_build_log_path(job_id:int) -> str:
    cwd = os.getcwd()
    runtime_path = get_runtime_dir_path(cwd)
    build_log_path = os.path.join(runtime_path, f'buildlog.txt')
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


def sleep_ms(ms:float) -> None:
    sleep(ms/1000.0)

def parse_args() -> list[str]:
    args = sys.argv[1:]
    piped = not sys.stdin.isatty()
    if piped:
        text = sys.stdin.read()
        return_args = args
    else:
        text = args[-1]
        return_args = args[:-1]
    filename = text    
    if os.path.isfile(filename):
        with open(filename, 'r') as f:
            text = f.read()
    return text, return_args

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
    path = unix_path(path)
    name = path.split('/')[-1]
    #plaintext file extensions that are not recognized by mimetype.guess_type() and will return (None, None) and so must be handled manually
    unknown_plaintext_extensions = ['swift', 'gitignore', 'diff', 'log']
    ext = name.split('.')[-1]
    if ext in unknown_plaintext_extensions:
        return True
    guess = guess_type(path, strict=False)[0]
    if guess and type(guess) == str:
        return guess[:4] == 'text'
    return False


    


def get_changed_file_paths():
    diff_command = 'git diff --name-only -z HEAD -- . :(exclude).gitignore'
    proc = subprocess.Popen(diff_command, stdout=subprocess.PIPE, shell=True)
    res, err = proc.communicate()
    if res:
        lines_bytes = [b for b in res.split(b'\x00') if b]
        file_paths = [b.decode() for b in lines_bytes]
        return file_paths
    else:
        print(f'Did not receive any results from: {diff_command}')
        print(f'Error message: {err.decode()}')
    return []


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

        





