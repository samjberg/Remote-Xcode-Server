import os
from mcp_utils import get_project_root_path

# cwd = os.getcwd()
cwd = get_project_root_path()
uploads_path = os.path.join(cwd, 'uploads')
if os.path.exists(uploads_path) and os.path.isdir(uploads_path):
    for name in os.listdir(uploads_path):
        os.remove(os.path.join(uploads_path, name))
    os.rmdir(uploads_path)


folder_paths = [os.path.join(cwd, name) for name in os.listdir() if (name != '__pycache__' and os.path.isdir(os.path.join(cwd, name)))]

for folder_path in folder_paths:
    for item in os.listdir(folder_path):
        os.remove(os.path.join(folder_path, item))
    os.rmdir(folder_path)


lines_to_remove = ['/uploads/', '/diffs/', 'uploads/', 'diffs/']
lines_to_write = []
gitignore_path = os.path.join(cwd, '.gitignore')
if os.path.exists(gitignore_path):
    with open(gitignore_path, 'r') as f:
        lines = f.readlines()
        for line in lines:
            if line.strip() not in lines_to_remove:
                lines_to_write.append(line + '\n')  #f.writelines does not add newline characters to the end of lines, so I am manually adding them back in here
    with open(gitignore_path, 'w') as f:
        f.writelines(lines_to_write)


