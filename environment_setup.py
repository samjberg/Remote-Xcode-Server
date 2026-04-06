import os
from sys import platform
from mcp_utils import ensure_directory_exists, get_runtime_dir_path, get_user_runtime_dir_path, unix_path, _normalize_path_for_compare
from subprocess import run

cwd = os.getcwd()
rxs_root_path = os.path.dirname(os.path.realpath(__file__)) #the root path where THIS SCRIPT is located.  Needed for locating the client script, which is in the same directory
runtime_dir_path = get_runtime_dir_path(cwd)
user_runtime_path = get_user_runtime_dir_path()
executables_path = os.path.join(user_runtime_path, 'executables')
client_script_name = 'mcp_client.py'
client_script_path = os.path.join(rxs_root_path, client_script_name)

project_bundles_path = os.path.join(user_runtime_path, 'project-bundles')



def update_windows_path(new_path_directory):
    import winreg, ctypes
    # Open the User Environment key
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Environment', 0, winreg.KEY_ALL_ACCESS) as key:
        try:
            current_path, _ = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            current_path = ""

        path_elements = [_normalize_path_for_compare(ele) for ele in current_path.split(os.pathsep) if ele.strip()]
        normalized_new_path = _normalize_path_for_compare(new_path_directory)

        # Avoid duplicates
        if normalized_new_path not in path_elements:
            updated_path = f"{new_path_directory};{current_path}"
            winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, updated_path)

            # Broadcast environment change without risking an indefinite hang.
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            send_result = ctypes.c_ulong()
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                5000,
                ctypes.byref(send_result),
            )

def update_posix_path(new_path_directory):
    user_home_path = os.path.expanduser('~')
    env_script_name = '.remote_xcode_env'
    env_script_path = os.path.join(user_home_path, env_script_name)
    new_path_directory = unix_path(new_path_directory) #just in case
    script_content = f'#!/bin/bash\nexport PATH="$PATH{os.pathsep}{new_path_directory}"\n'
    #creates a centralized script for updating PATH which lives in runtime_dir
    with open(env_script_path, 'w') as f:
        f.write(script_content)

    #inject the env_script into the most common profiles, .bashrc and .zshrc
    bashrc_path = os.path.join(user_home_path, '.bashrc')
    zshrc_path = os.path.join(user_home_path, '.zshrc')
    profile_path = os.path.join(user_home_path, '.profile')
    path_injection_str = f'[[ -f ~/{env_script_name} ]] && source ~/{env_script_name}'

    profile_candidates = [bashrc_path, zshrc_path, profile_path]

    for path in profile_candidates:
        if os.path.exists(path):
            with open(path, 'r') as f:
                lines = [line.strip() for line in f.readlines()]
        else:
            lines = []
        if path_injection_str not in lines:
            with open(path, 'a') as f:
                f.write(path_injection_str + '\n')




def ensure_environment_setup():
    #Create all necessary directories if they do not exist
    #first create all necesesary directories (if they do not exist) within project root
    if not os.path.exists(runtime_dir_path):
        os.makedirs(runtime_dir_path)
    else:
        if not os.path.isdir(runtime_dir_path):
            os.remove(runtime_dir_path)
            os.makedirs(runtime_dir_path)


    #next create all necessary directories in user home path (global info for all projects such as serverinfo)
    if not os.path.exists(user_runtime_path):
        os.makedirs(user_runtime_path)
    else:
        #case where for some reason user_runtime_path DOES exist, but is a file instead of directory.  Delete it and create it as directory
        if not os.path.isdir(user_runtime_path):
            os.remove(user_runtime_path)
            os.makedirs(user_runtime_path)

    certs_path = os.path.join(user_runtime_path, 'certs')
    credentials_path = os.path.join(user_runtime_path, 'credentials')
    #ensure existence of certs directory (~/.remote-xcode-server/certs)
    if not os.path.exists(certs_path):
        os.makedirs(certs_path)
    else:
        if not os.path.isdir(certs_path):
            os.remove(certs_path)
            os.makedirs(certs_path)

    #ensure existence of credentials directory (~/.remote-xcode-server/credentials)
    if not os.path.exists(credentials_path):
        os.makedirs(credentials_path)
    else:
        if not os.path.isdir(credentials_path):
            os.remove(credentials_path)
            os.makedirs(credentials_path)

    #ensure existence of executables directory
    if not os.path.exists(executables_path):
        os.makedirs(executables_path)
    else:
        if not os.path.isdir(executables_path):
            os.remove(executables_path)
            os.makedirs(executables_path)


    #determine whether this is running on a posix or windows environment
    posix_os = os.name == 'posix' or platform == 'darwin'

    #Ensure that executables_path is in the system PATH (this is what enables xcodebuild to register as a command)
    curr_path = os.environ['PATH']
    path_elements = [_normalize_path_for_compare(path) for path in curr_path.split(os.pathsep) if path.strip()]
    normalized_executables_path = _normalize_path_for_compare(executables_path)
    if normalized_executables_path not in path_elements:
        if posix_os:
            update_posix_path(executables_path)
        else:
            update_windows_path(executables_path)



    #if this is a windows machine
    if not posix_os:
        shim_script_name = 'xcodebuild.cmd'
        shim_script_path = os.path.join(executables_path, shim_script_name)
        shim_script_content = f'@echo off\npython "{client_script_path}" build %*'
    #if this is a posix machine
    else:
        shim_script_name = 'xcodebuild'
        shim_script_path = os.path.join(executables_path, shim_script_name)
        shim_script_content = f'#!/bin/bash\npython "{client_script_path}" build "$@"'


    with open(shim_script_path, 'w') as f:
        f.write(shim_script_content + '\n')

    if posix_os:
        proc = run(['chmod', '+x', shim_script_path])
        if proc.returncode != 0:
            print(f'Error running command: chmod +x {shim_script_path}')

    #ensure project bundles directory exists (~/.remote-xcode-server/project-bundles)
    ensure_directory_exists(project_bundles_path)


if __name__ == '__main__':
    ensure_environment_setup()
