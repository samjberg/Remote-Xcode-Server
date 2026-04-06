from logging import root
from mcp_utils import *


def rmrf(root_path):
    for dirpath, dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            if not os.path.isabs(filename):
                filename = os.path.join(dirpath, filename)
            path = filename if (os.path.isabs(filename) and os.path.exists(filename)) else os.path.join(dirpath, filename)
            os.remove(path)

    for dirpath, dirnames, filenames in os.walk(root_path):
        for dirname in dirnames:
            path = dirname if (os.path.isabs(dirname) and os.path.exists(dirname)) else os.path.join(dirpath, dirname)
            os.rmdir(path)

    if os.path.exists(root_path):
        if os.path.isdir(root_path):
            os.rmdir(root_path)
        elif os.path.isfile(root_path):
            os.remove(root_path)



if __name__ == '__main__':
    cwd = os.getcwd()
    homedir = os.path.expanduser('~')
    coding_dir = os.path.join(homedir, 'Codingstuff')
    python_dir = os.path.join(coding_dir, 'python')
    user_runtime_dir = os.path.join(homedir, '.remote-xcode-server')

    # rmrf(user_runtime_dir)
    rxs_path = os.path.join(python_dir, 'Remote-Xcode-Server')
    rxs_runtime_dir = os.path.join(rxs_path, '.remote-xcode-server')

    testing_dir = os.path.join(coding_dir, 'Testing')
    xrstesting_path = os.path.join(testing_dir, 'xrs-testing')


    if os.path.exists(user_runtime_dir):
        if os.path.isdir(user_runtime_dir):
            print(f'Deleting {user_runtime_dir}')
            rmrf(user_runtime_dir)

    if os.path.exists(rxs_runtime_dir):
        if os.path.isdir(rxs_runtime_dir):
            print(f'Deleting {rxs_runtime_dir}')
            rmrf(rxs_runtime_dir)

    if os.path.exists(xrstesting_path):
        if os.path.isdir(xrstesting_path):
            print(f'Deleting {xrstesting_path}')
            rmrf(xrstesting_path)

