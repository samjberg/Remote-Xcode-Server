import sys, os, socket, requests, json
from requests import Response
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

#Sort of like git push, but for uncommited changes and specifically from the server
def send_current_changes(server_addr:tuple[str, int]) -> Response:
    git_diff_path, changed_binary_paths = prepare_text_changes()

    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/sendchanges/{app_name}'
    filename = 'gitdiff.diff'

    files = {'gitdiff': (filename, open(git_diff_path, 'rb'), 'text/plain', {'Expires': 0})}

    
    for i, path in enumerate(changed_binary_paths):
        path = unix_path(path)
        print(f'Adding {path} to POST request')
        mimetype, encoding = guess_type(path, strict=False)
        if not mimetype:
            mimetype = 'application/octet-stream'
        files[f'binaryfile{i}'] = (path, open(path, 'rb'), mimetype, {'Expires': 0})
    print(f'Sending changes located at {git_diff_path} to {url}\n')
    resp = requests.post(url, files=files)
    return resp


#Sort of like git pull, but for uncommitted changes and specifically to the server
def retrieve_current_changes(server_addr:tuple[str, int]) -> bool:
    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/retrieve_text_changes/{app_name}'
    ran_successfully = True
    try:
        diff_resp:Response = requests.get(url, stream=True)
        diff_resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve text changes: {e}')
        return False

    if not diff_resp.content:
        print('Received empty diff file')

    runtime_dir = get_runtime_dir_path()
    git_patch_path = os.path.join(runtime_dir, 'gitdiff.diff')
    with open(git_patch_path, 'wb') as f:
        f.write(diff_resp.content)

    #apply patch
    git_apply_command = f'git apply {git_patch_path}'
    os.system(git_apply_command)

    #request paths for changed binary files
    url = f'http://{ip}:{port}/retrieve_changed_binary_paths/{app_name}'
    try:
        binary_paths_resp:Response = requests.get(url)
        binary_paths_resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve binary path list: {e}')
        return False
    if not binary_paths_resp.text:
        print('No changed binary files returned by server')

    #split binary file paths into a list
    paths = [path.strip() for path in binary_paths_resp.text.split('\n')]
    paths = [path for path in paths if path] #remove empty paths

    for path in paths:
        #verify that the directory in which we are supposed to write the binary file to already exists.  If not, create it and all intermediate directories with os.makedirs
        parent_dir = os.path.dirname(path)
        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        filename = os.path.split(path)[-1]
        print(f'Retrieving {filename} from server')
        sanitized_path = sanitize_path_for_url(path)
        url = f'http://{ip}:{port}/retrieve_binary_file/{app_name}/{sanitized_path}'
        try:
            resp:Response = requests.get(url, stream=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f'Failed to retrieve binary file {path}: {e}')
            ran_successfully = False
            continue
        chunk_size = 1024 * 8
        with open(path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)
    
    return ran_successfully #the idea is just to return True/False based on whether everything runs successfully or not, but I haven't really implemented that yet
    




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




    if len(sys.argv) > 1:
        arg = sys.argv[1]
    else:
        arg = 'build'


    if 'build' in arg: #for now, just to allow for flexibility in testing
        git_diff_filepath, changed_binary_paths = prepare_text_changes()
        resp:Response = start_build_job(server_addr, git_diff_filepath, changed_binary_paths)
        json_obj = json.loads(resp.text)
        job_id = json_obj['job_id']
        build_log_str = wait_for_build_completion(server_addr, job_id)
        # print('final build log')
        # print(build_log_str)
    elif 'sendchanges' in arg:
        resp:Response = send_current_changes(server_addr)
        print(resp)
    elif 'getchanges' in arg:
        success:bool = retrieve_current_changes(server_addr)
        if success:
            print('Successfully retrieved changes from the server')
        else:
            print('Failed to retrieve changes from the server')
    else:
        print(f'Invalid argument: {arg}')
    
