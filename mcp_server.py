import os, subprocess, socket
from flask import Flask, request, send_file, send_from_directory, jsonify, Request, Response
from threading import Thread
from werkzeug.utils import secure_filename
from uuid import uuid4
from mcp_utils import *
# from requests import Request



cwd = unix_path(os.getcwd())
project_name = get_project_name()
server_dir_name = get_runtime_dir_name()
server_dir_path = os.path.join(cwd, server_dir_name)
project_info_filename = 'projectinfo.txt'
project_info_filepath = os.path.join(server_dir_path, project_info_filename)

server_socket_port = 50271 
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(('0.0.0.0', server_socket_port))
server.listen(1)

update_gitignore()



if not uploads_folder_exists():
    os.mkdir(server_dir_path)

if project_info_filename not in os.listdir(server_dir_path): #this means this is the first time the server is being run, 
        with open(project_info_filepath, 'w') as f:
            config_lines = [f'project_root:{cwd}\n', f'project_name:{project_name}\n']
            for line in config_lines:
                if line[-1] in '\n\r':
                    f.write(line)
                else:
                    f.write(line + '\n')
            # f.writelines(config_lines)






UPLOAD_FOLDER = unix_path(os.path.join(cwd, server_dir_name))
JOBS = {}
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER




def get_safe_project_path(client_path:str) -> str:
    """Resolve a client-supplied repo-relative path safely under the server project root."""
    if client_path is None:
        raise ValueError('Path is None')

    rel_path = unix_path(str(client_path)).strip()
    if rel_path == '':
        raise ValueError('Path is empty')
    if '\x00' in rel_path:
        raise ValueError('Path contains NUL byte')
    if rel_path.startswith('/'):
        raise ValueError(f'Absolute paths are not allowed: {rel_path}')

    # Reject Windows drive-letter paths (e.g. C:/foo) and UNC-like inputs.
    first_part = rel_path.split('/')[0]
    if len(first_part) >= 2 and first_part[1] == ':':
        raise ValueError(f'Drive-letter paths are not allowed: {rel_path}')
    if rel_path.startswith('//'):
        raise ValueError(f'UNC-like paths are not allowed: {rel_path}')

    normalized_rel = unix_path(os.path.normpath(rel_path))
    normalized_parts = [p for p in normalized_rel.split('/') if p not in ['', '.']]
    if any(p == '..' for p in normalized_parts):
        raise ValueError(f'Path traversal is not allowed: {rel_path}')

    project_root_abs = os.path.abspath(cwd)
    dest_abs = os.path.abspath(os.path.join(project_root_abs, normalized_rel))
    if os.path.commonpath([project_root_abs, dest_abs]) != project_root_abs:
        raise ValueError(f'Path escapes project root: {rel_path}')

    return dest_abs




def run_xcodebuild(job_id):
    conn, addr = server.accept()
    print(f'Received connection from {addr}')
    try:
        job = JOBS[job_id]
        job['status'] = 'running'
        xcodebuild_command: str = f"xcodebuild -scheme \"{project_name}\" -destination 'generic/platform=iOS Simulator' build"
        proc = subprocess.Popen(xcodebuild_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, shell=True)
        chunk_size = 4096
        log_write = job['file']
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            log_write.write(chunk.decode())
            conn.sendall(chunk)

        log_write.close()
        proc.wait()
        conn.close()

    except Exception as e:
        JOBS[job_id]['status'] = 'error'
        JOBS[job_id]['error'] = str(e)

@app.route('/retrieve_text_changes/<appname>')
def send_changes(appname:str):
    git_diff_path, changed_binary_paths = prepare_text_changes()
    git_diff_dir, git_diff_name = [unix_path(p) for p in os.path.split(git_diff_path)]
    return send_from_directory(git_diff_dir, git_diff_name, as_attachment=True)

@app.route('/retrieve_changed_binary_paths/<appname>')
def send_changed_binary_paths(appname:str):
    changed_file_paths = [path for path in get_changed_file_paths() if path]
    changed_binary_paths = [path for path in changed_file_paths if not is_plaintext(path.split('/')[-1])]
    paths_str = '\n'.join(changed_binary_paths)
    return paths_str


#note that <path:path> is NOT representing <variablename:variablename>. The full syntax for route variables is <converter:name>
#so it is just a coincidence that the thing I'm trying to pass here as a variable in the url IS literally a path, which happens to be
#the same as the name of the converter we need to use: "path".  The path converter makes it so that we can pass nested paths here, instead
#of paths getting cut off once they reach the first '/' (the first path delimiter)
@app.route('/retrieve_binary_file/<appname>/<path:path>')
def send_binary_file(appname:str, path:str):
    path = get_safe_project_path(path)
    if not os.path.exists(path):
        filename = os.path.split(path)[-1]
        return f'Invalid path/requested file: {filename} does not exist'
    elif not os.path.isfile(path):
        filename = os.path.split(path)[-1]
        if os.path.isdir(path):
            return f'Path: {path} exists, but is a directory.  Only individual binary files can be requested via this route'
        else:
            return f'Path: {path} exists, but is somehow neither a regular file or directory.  You did something really crazy.'

    return send_file(path, as_attachment=True)


@app.route('/sendchanges/<appname>', methods=['POST'])
def receieve_changes(appname):
    if 'gitdiff' not in request.files:
        print('No diff received')
        return 'No diff received'

    file = request.files['gitdiff']
    print(f'file: {file}')
    print(f'file.filename: {file.filename}')
    print(f'file.name: {file.name}')
    if file == '':
        print('empty filename')
        return 'empty filename'

    print(f'request.files: {request.files}')

    #there are are additional file(s) besides the diff.  This means the client sent binary files
    #we need to save these files to their paths (path is the first item of the tuple)
    # for i in range(1, len(request.files.keys())):
    for file_key in request.files.keys():
        if file_key == 'gitdiff':
            continue
        binary_file = request.files[file_key]
        #I know this seems wrong.  But FileStorage.filename always returns the FIRST ITEM (0 index) in the tuple that was 
        #used as the value in the files dict sent by the requests library (from the client).  And for the binary files, I am
        #passing the path in the 0th index instead of the filename, because I need to save the files in the same relative locations
        rel_path = unix_path(binary_file.filename)
        binary_file_name = rel_path.split('/')[-1]
        path = get_safe_project_path(rel_path)
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        if os.path.exists(path):
            if os.path.isfile(path):
                #If the path exists and IS a a file, simply remove it, since FileStorage.save does not overwrite files apparently
                os.remove(path)
            else:
                print(f'Path given for changed or added binary file {binary_file_name}: {path}, already exists as a directory')
                print("Honestly, that's just really strange.  Idk")

        binary_file.save(path)

    if file and allowed_filename(file.filename):
        #create a secure version of the filename
        filename = secure_filename(file.filename)
        #save the file with the secure filename in UPLOAD_FOLDER
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        patch_path = f'{app.config["UPLOAD_FOLDER"]}/{filename}'
        if os.path.getsize(patch_path) > 0:
            git_apply_command = f'git apply {patch_path}'
            #run the git apply command
            os.system(git_apply_command)
    else:
        return f"Disallowed filename or file does not exist"

    return "Some other method besides POST or GET was used.  Don't do that"



@app.route('/appname/<appname>', methods=['GET', 'POST'])
def start_build_job(appname):
    print(f'appname: {appname}')

    if request.method == 'POST' or request.method == 'GET':
        if 'gitdiff' not in request.files:
            print('NO FILE PART')
            return 'NO FILE PART'

        file = request.files['gitdiff']
        print(f'file: {file}')
        print(f'file.filename: {file.filename}')
        print(f'file.name: {file.name}')
        if file == '':
            print('empty filename')
            return 'empty filename'

        print(f'request.files: {request.files}')

        #there are are additional file(s) besides the diff.  This means the client sent binary files
        #we need to save these files to their paths (path is the first item of the tuple)
        # for i in range(1, len(request.files.keys())):
        for file_key in request.files.keys():
            if file_key == 'gitdiff':
                continue
            binary_file = request.files[file_key]
            #I know this seems wrong.  But FileStorage.filename always returns the FIRST ITEM (0 index) in the tuple that was 
            #used as the value in the files dict sent by the requests library (from the client).  And for the binary files, I am
            #passing the path in the 0th index instead of the filename, because I need to save the files in the same relative locations
            rel_path = unix_path(binary_file.filename)
            binary_file_name = rel_path.split('/')[-1]
            path = get_safe_project_path(rel_path)
            parent_dir = os.path.dirname(path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            if os.path.exists(path):
                if os.path.isfile(path):
                    #If the path exists and IS a a file, simply remove it, since FileStorage.save does not overwrite files apparently
                    os.remove(path)
                else:
                    print(f'Path given for changed or added binary file {binary_file_name}: {path}, already exists as a directory')
                    print("Honestly, that's just really strange.  Idk")

            binary_file.save(path)



        if file and allowed_filename(file.filename):
            #create a secure version of the filename
            filename = secure_filename(file.filename)
            #save the file with the secure filename in UPLOAD_FOLDER
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

            patch_path = f'{app.config["UPLOAD_FOLDER"]}/{filename}'
            if os.path.getsize(patch_path) > 0:
                git_apply_command = f'git apply {patch_path}'
                #run the git apply command
                os.system(git_apply_command)


            job_id = str(uuid4())
            if job_id in JOBS.keys():
                return f'<p>Already building {appname}, job_id: {job_id}</p>'

            build_log_name:str = f'buildlog-{job_id}.txt'
            build_log_path:str = os.path.join(UPLOAD_FOLDER, build_log_name)

            #Create the new job object and put it in job_id in the JOBS dict.
            #We have to be careful to ensure the file gets closed
            build_log_file = open(build_log_path, 'w')
            JOBS[job_id] = {"status": "pending", "result": '', "error": None, "file": build_log_file}

            t = Thread(target=run_xcodebuild, args=([job_id]), daemon=True)
            t.start()
            return jsonify({"job_id": job_id}), 202
        else:
            return 'No file, or disallowed file type was uploaded'
    else:
        return "Some other method besides POST or GET was used.  Don't do that"
        

@app.route('/retrieve_changed_file_paths/<appname>')
def send_changed_file_paths(appname:str) -> Response:
    #run "git add ." 
    get_changed_file_paths()
    os.system('git add .')



@app.route('/checkprogress/<job_id>/<offset>')
def check_progress(job_id:str, offset:int) -> Response:
    job = JOBS[job_id]
    if job['status'] == 'done':
        return 'Build already Complete'

    build_log_path = get_build_log_path(job_id)
    if not os.path.exists(build_log_path):
        return 'Error: build job does not exist.'
    with open(build_log_path, 'r') as f:
        if f.seekable():
            f.seek(int(offset), 0)
        else:
            f.read(offset)
        new_text = f.read()

    job['result'] += new_text
    return jsonify({'job_id': job_id, 'status': 'pending', 'newtext': new_text, 'result': job['result']})








@app.route('/status/<job_id>')
def job_status(job_id):
    if job_id not in JOBS:
        job_id = str(uuid4())
    job = JOBS.get(job_id)
    if not job:
        return jsonify("error", "job not found"), 404
    return jsonify(job)

    



@app.route('/')
def hello_world():
    return 'Hello, World!'



if __name__ == '__main__':
    ip, port = '0.0.0.0', get_server_port()
    app.run(ip, port)








