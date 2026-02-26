import sys, os, socket, requests, json
from flask import Response
from mcp_utils import *

def get_jobid_from_resp(resp:Response):
    return json.loads(resp.text)['job_id']

def start_build_job(server_addr:tuple[str, int], git_diff_path:str, changed_binary_paths:list[str]=[]) -> str:
    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/appname/{app_name}'
    filename = git_diff_path.split('/')[-1]

    files = {'gitdiff': (filename, open(git_diff_path, 'rb'), 'text/plain', {'Expires': 0})}
    for i, path in enumerate(changed_binary_paths):
        path = unix_path(path)
        print(f'Adding {path} to POST request')
        mimetype, encoding = guess_type(path, strict=False)
        if not mimetype:
            mimetype = 'application/octet-stream'
        files[f'binaryfile{i}'] = (path, open(path, 'rb'), mimetype, {'Expires': 0})
    print(f'Starting build job by making POST request to {url} sending a diff file located at {git_diff_path}\n')
    resp = requests.post(url, files=files)
    return resp

def check_build_job(server_addr:tuple[str, int], job_id:str, offset:int=0) -> Response:
    ip, port = server_addr
    url = f'http://{ip}:{port}/checkprogress/{job_id}/{offset}'
    resp = requests.get(url)
    return resp

def wait_for_build_completion(server_addr:tuple[str, int], job_id:str, offset=0) -> str:
    ip, port = server_addr
    server_socket_port = 50271
    chunk_size = 4096
    full_text = ''
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((ip, server_socket_port))
        while True:
            new_bytes = s.recv(chunk_size)
            if not new_bytes:
                break
            new_text = new_bytes.decode('utf-8')
            print(new_text, end='')
            full_text += new_text

    return full_text


#for now, temporarily, I will just impose the requirement that the client must be run from the project root.  This will let me get a testable version much more quickly.  I can improve it from there


if __name__ == '__main__':

    cwd = unix_path(os.getcwd())
    server_ip, server_port = '192.168.7.189', get_server_port() 
    server_addr = (server_ip, server_port)
    BUILD_SUCCESS = '** BUILD SUCCEEDED **'
    BUILD_FAILED = '** BUILD FAILED **'



    runtime_dir = get_runtime_dir_name()
    diffs_path = unix_path(os.path.join(cwd, runtime_dir))
    gitignore_path = os.path.join(cwd, '.gitignore')
    git_diff_filepath = unix_path(os.path.join(diffs_path, 'gitdiff.diff'))
    git_add_command = 'git add .'



    update_gitignore()


    #if 
    if os.path.exists(diffs_path):
        if not os.path.isdir(diffs_path): #diffs_path exists but is a file instead of a directory.  Delete it, and a make a directory in its place
            os.remove(diffs_path)
            os.mkdir(diffs_path)
    else:
        os.mkdir(diffs_path)
    #End of all first-run initialization

    #Actuall run the git commands
    os.system(git_add_command)

    changed_file_paths = [path for path in get_changed_file_paths() if path]
    changed_binary_paths = [path for path in changed_file_paths if not is_plaintext(path.split('/')[-1])]
    changed_text_paths = [path for path in changed_file_paths if path not in changed_binary_paths and path != '.gitignore']

    # Build a patch that only contains plaintext files; binary files are sent separately.
    with open(git_diff_filepath, 'w', newline='') as diff_file:
        if changed_text_paths:
            subprocess.run(['git', 'diff', 'HEAD', '--', *changed_text_paths], stdout=diff_file)

    for path in changed_file_paths:
        print('Changed ', end='')
        if path in changed_binary_paths:
            print('binary ', end='')
        print(f'file located at {path}')

    resp:Response = start_build_job(server_addr, git_diff_filepath, changed_binary_paths)
    json_obj = json.loads(resp.text)
    job_id = json_obj['job_id']
    build_log_str = wait_for_build_completion(server_addr, job_id)
    # print('final build log')
    print(build_log_str)
    
