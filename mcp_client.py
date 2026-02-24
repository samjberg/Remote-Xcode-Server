import sys, os, socket, requests, json
from flask import Response
from mcp_utils import *


args = sys.argv[1:]
cwd = unix_path(os.getcwd())
app_name = get_appname()
server_ip, server_port = '192.168.7.189', get_server_port() 
server_addr = (server_ip, server_port)
BUILD_SUCCESS = '** BUILD SUCCEEDED **'
BUILD_FAILED = '** BUILD FAILED **'



if len(args) == 0:
    args.append(app_name)





diffs_path = unix_path(os.path.join(cwd, 'diffs'))
gitignore_path = os.path.join(cwd, '.gitignore')
git_diff_filepath = unix_path(os.path.join(diffs_path, 'gitdiff.diff'))
git_diff_command = 'git diff HEAD'
full_git_diff_command = f'{git_diff_command} > "{git_diff_filepath}"'
git_add_command = 'git add .'
current_offset = 0



#Handle all first-run initialization
if not os.path.exists(gitignore_path):
    with open(gitignore_path, 'w') as f:
        f.write('/diffs/') #we want just this literal string, this is being written to .gitignore
        # f.write(f'/{diffs_path}/\n')


#if 
if os.path.exists(diffs_path):
    if not os.path.isdir(diffs_path): #diffs_path exists but is a file instead of a directory.  Delete it, and a make a directory in its place
        os.rm(diffs_path)
        os.mkdir(diffs_path)
else:
    os.mkdir(diffs_path)
#End of all first-run initialization




def get_jobid_from_resp(resp:Response):
    return json.loads(resp.text)['job_id']

def start_build_job(server_addr:tuple[str, int], git_diff_path) -> str:
    ip, port = server_addr
    url = f'http://{ip}:{port}/appname/{app_name}'
    filename = git_diff_path.split('/')[-1]
    files = {'file': (filename, open(git_diff_path, 'rb'), 'text/plain', {'Expires': 0})}
    print(f'Starting build job by making POST request to {url} sending a diff file located at {git_diff_filepath}\n')
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

    # job_finished = False
    # sleep_ms(20)
    # while not job_finished:
    #     resp = check_build_job(server_addr, job_id, offset)
    #     # print(f'resp: {resp}')
    #     # print(f'resp.text: {resp.text}')
    #     job = json.loads(resp.text)
    #     new_text = job['newtext']
    #     print(new_text, end='')
    #     offset += len(new_text)
    #     # print(f'new build text:  {new_text}', end='')
    #     if (job['status'].lower() == 'done') or (job['status'].lower() == 'complete'):
    #         job_finished = True
    #         return job['result']
    #     sleep_ms(1000)




#for now, temporarily, I will just impose the requirement that the client must be run from the project root.  This will let me get a testable version much more quickly.  I can improve it from there






os.system(git_add_command)
# sleep_ms(10) ###  sleep is NOT necessary.  os.system is blocking, so even though it DOES launch a new process (subshell), the main process (this script) is blocked until it complete.  Therefore there is no race condition by definition.
os.system(full_git_diff_command)
# sleep_ms(50)












if __name__ == '__main__':
    resp:Response = start_build_job(server_addr, git_diff_filepath)
    print(f'resp: {resp}')
    print(f'resp.text: {resp.text}')
    json_obj = json.loads(resp.text)
    print(f'json_obj: {json_obj}')
    job_id = json_obj['job_id']
    build_log_str = wait_for_build_completion(server_addr, job_id)
    print('final build log')
    print(build_log_str)
    


