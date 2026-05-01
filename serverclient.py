
import os, sys, json

from mcp_utils import normalize_path_for_compare
from parseargs import parse_args
from mcp_utils import *
import shutil

from projects_context_manager import load_projects_dict

#this file is for running commands locally on the server which affect things on the server.  I.e. acting as a "local client" to the server
#for operations such as manually removing a tracked project, or changing its path, or whatever

server_dir_path = get_server_dir_path()
tracked_projects_file_path = os.path.join(server_dir_path, 'tracked_projects.csv')



def remove_tracked_project(name: str, delete_repo=True):
    with open(tracked_projects_file_path, 'r') as f:
        dct: dict[str, dict[str, str]] = json.load(f)
    # if name in dct:
    #     project_root_path = dct[name]['project_root_path']
    #     if delete_repo and os.path.exists(project_root_path):
    #         shutil.rmtree(project_root_path)
    #     del dct[name]

    normed_name = normalize_path_for_compare(name)

    entry_id = ''
    #exact given name failed to find in the dict.  Manually iterate through and check if normalized names gives a match
    for project_id, project_dict in dct.items():
        dictname = project_dict['project_name']
        normed_dict_name = normalize_path_for_compare(dictname)
        if name == dictname or normed_name == normed_dict_name:
            entry_id = project_id
            break

    if entry_id:
        project = dct[entry_id]
        if delete_repo:
            project_root_path = project['project_root_path']
            shutil.rmtree(project_root_path)
        del dct[entry_id]
        with open(tracked_projects_file_path, 'r') as f:
            json.dump(dct, f)


def list_projects():
    dct: dict[str, dict[str, str]] = load_projects_dict()
    for _, project in dct.items():
        project_name = project['project_name']
        project_root_path = project['project_root_path']
        print(f'{project_name}: {project_root_path}')


valid_commands = []



if __name__ == '__main__':

    args_dict = parse_args(sys.argv[1:])
    long_flags = args_dict['long']
    short_flags = args_dict['short']
    plain_args = args_dict['args']

    print(f'{short_flags}\n\n{long_flags}\n\n{plain_args}')

    command = plain_args[0]
    if len(plain_args) > 1:
        project_name = plain_args[1]
    else:
        project_name = ''

    match command:
        case 'remove':
            if 'd' in short_flags:
                print(f'Removing {project_name} from tracked projects and deleting local repo on server')
                remove_tracked_project(project_name, True)
            else:
                print(f'Removing {project_name} from tracked projects')
                remove_tracked_project(project_name, False)

        case 'listprojects':
            list_projects()







































