import os, sys, pathlib
from mcp_utils import get_project_root_path, get_runtime_dir_path

cwd = os.getcwd()
project_root_path = get_project_root_path(cwd)
runtime_dir_path = get_runtime_dir_path(cwd)
executables_path = os.path.join(runtime_dir_path, 'executables')

if __name__ == '__main__':
    #Create all necessary directories if they do not exist
    if not os.path.exists(runtime_dir_path):
        os.makedirs(runtime_dir_path)
    else:
        if not os.path.isdir(runtime_dir_path):
            os.remove(runtime_dir_path)

    if not os.path.exists(executables_path):
        os.makedirs(executables_path)
    else:
        if not os.path.isdir(executables_path):
            os.makedirs(executables_path)
