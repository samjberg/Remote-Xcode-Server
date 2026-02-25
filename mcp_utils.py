import os, sys, pathlib, socket
from time import sleep
from typing import BinaryIO

server_port = 8751
socket_port = 50682

def get_server_port() -> int:
    return server_port

def standardize_project_name(name:str) -> str:
    name.replace()

def unix_path(path:str) -> str:
    return pathlib.Path(path).as_posix()

def allowed_filename(filename:str) -> bool:
    valid_extensions = ['diff', 'txt']
    return filename.rsplit('.', 1)[-1].lower() in valid_extensions

def get_build_log_path(job_id:int) -> str:
    cwd = os.getcwd()
    uploads_path = os.path.join(cwd, 'uploads')
    build_log_path = os.path.join(uploads_path, f'buildlog.txt')
    return build_log_path


def uploads_folder_exists() -> bool:
    cwd = os.getcwd()
    if 'uploads' not in os.listdir(cwd):
        return False
    return os.path.isdir(os.path.join(cwd, 'uploads'))

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
    ignores_uploads = False
    ignores_diffs = False
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, 'w') as f:
            f.write('/uploads/\n/diffs/\n')
    else:
        ends_with_newline = False
        with open('.gitignore', 'r') as f:
            for line in f.readlines():
                for s in ['/uploads/', 'uploads/']:
                    if s == line or s in line:
                        ignores_uploads = True
                for s in ['/diffs/', 'diffs/']:
                    if s == line or s in line:
                        ignores_diffs = True
                if line[-1] == '\n':
                    ends_with_newline = True

        if not (ignores_uploads and ignores_diffs): #as long as BOTH are NOT already ignored
            with open(gitignore_path, 'a') as f:
                if not ends_with_newline:
                    f.write('\n')
                if not ignores_uploads:
                    f.write('/uploads/\n')
                if not ignores_diffs:
                    f.write('/diffs/\n')








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

        





