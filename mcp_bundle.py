# import os, subprocess, shlex
from mcp_utils import *
from mcp_client import send_files

args = sys.argv[1:]
from_ref = args[0]
to_ref = args[1] if len(args) > 1 else 'HEAD'
server_addr = ('192.168.7.189', get_server_port())


bundle_path = git_create_update_bundle(from_ref, to_ref, 'update.bundle')
success = send_files(server_addr, bundle_path)

if success:
    print(f'SUCCESS: Sent bundle located at {os.path.abspath(bundle_path)} to server')
else:
    print(f'ERROR: Failed to send bundle located at {os.path.abspath(bundle_path)} to server')
# EXECUTABLE_PATHS = {'git': '/Program Files/Git/bin/git.exe'}

# def run_command(cmd:list[str]|str, stdout=subprocess.PIPE, stderr=subprocess.PIPE) -> str:
#     if isinstance(cmd, str):
#         cmd = shlex.split(cmd)
#     print(f'running: {cmd}')
#     proc = subprocess.run(cmd, stdout=stdout, stderr=stderr)
#     proc.check_returncode()
#     try:
#         res = proc.stdout.decode(errors='replace')
#     except UnicodeDecodeError as e:
#         print(f'Error decoding process output: {e}')
#     return res



# def apply_incremental_git_bundle(bundle_path:str, update_and_create_branches:bool=True) -> bool:
#     heads_part = '+refs/heads/*:refs/heads/*'
#     tags_part = '+refs/tags/*:refs/tags/*'
#     command_str = f"git fetch {bundle_path} '{heads_part}' '{tags_part}'"
#     command = shlex.split(command_str)
#     run_command(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
#     pass




# def get_commits_between(start_ref:str, end_ref:str='HEAD') -> list[str]:
#     cmd = shlex.split(f'git rev-list {start_ref} ^{end_ref}')
#     proc:subprocess.CompletedProcess = _run_git_capture(cmd)
#     proc.check_returncode()
#     try:
#         res_str:str = proc.stdout.decode(errors='replace')
#     except UnicodeDecodeError as err:
#         print(f'Failed to decode output for command: {cmd}\nError message: {err}')
#     commits = [line.strip() for line in res_str.splitlines()]
#     return commits

# def create_update_bundle(start_ref:str, end_ref:str='HEAD', save_path='update.bundle') -> str:
#     cmd = shlex.split(f'git bundle create {save_path} {start_ref}..{end_ref} --all')
#     proc = _run_git_capture(cmd)
#     proc.check_returncode()
#     try:
#         res = proc.stdout.decode(errors='replace')
#     except UnicodeDecodeError as err:
#         print(f'Error decoding output from command: {' '.join(cmd)}\nError Message: {err}')
#     return save_path

# def git_verify_bundle(bundle_path:str) -> tuple[bool, list[str]]:
#     '''Verifies that the git bundle'''
#     project_root = get_project_root_path()
#     if not is_subdir(bundle_path, project_root):
#         raise FileNotFoundError('')
#     verify_bundle_command = shlex.split(f'git bundle verify {bundle_path}')
#     proc:subprocess.CompletedProcess = _run_git_capture(verify_bundle_command)
#     proc.check_returncode()
#     if not proc:
#         print(f'git bundle verify {bundle_path} produced no output')
#     try:
#         res:str = proc.stdout.decode(errors='replace')
#     except UnicodeDecodeError as err:
#         print(f'Error decoding output from command {' '.split(verify_bundle_command)}\nError Message: {err}')
    
#     lines = res.splitlines()
#     for line in lines:
#         if line.startswith('error'):
#             return False
#     return True




# result = get_commits_between('HEAD', '61b9896')
# lst = [line.strip() for line in result.splitlines()]
# print('results:')
# for line in lst:
#     print(line)


