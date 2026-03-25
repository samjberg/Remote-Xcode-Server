from mcp_utils import *


ADDLEFT = 'addleft'
SUBLEFT = 'subleft'
ADDRIGHT = 'addright'
SUBRIGHT = 'subright'
CHANGELINE = 'changeline'


class Change:
    def __init__(self, line_start, line_end, offset, text, changetype):
        self.line_start = line_start
        self.line_end = line_end
        self.offset = offset
        self.text = text
        self.changetype = changetype







if __name__ == '__main__':
    client_version_path, server_version_path = sys.argv[1:]

    with open(client_version_path, 'r') as f:
        client_text = f.read()
    
    with open(server_version_path, 'r') as f:
        server_text = f.read()


    if client_text == server_text:
        print('No action needed, files are identical')
        exit()

    client_lines = client_text.splitlines()
    server_lines = server_text.splitlines()


    if len(client_lines) >= len(server_lines):
        left_lines, right_lines = client_lines, server_lines
    else:
        left_lines, right_lines = server_lines, client_lines

    
    llo = 0 #left line offset
    rlo = 0 #right line offset

    changes = []


    for i in range(len(left_lines)):
        left_line = left_lines[i+llo]
        right_line = right_lines[i+rlo]
        if left_line != right_line:
            if left_line in right_lines[i:]:
                #this proves that the change is additive (to the left), because left is longer, and the current left line exists somewhere further down on the right side
                start_line = i + rlo
                end_line = right_lines.index(left_line, i) + rlo
                change_text = '\n'.join(left_lines[start_line:end_line])
                change = Change(start_line, end_line, rlo, change_text, ADDRIGHT)
                changes.append(change)
            elif right_line in left_lines[i:]:
                start_line = i + llo
                end_line = left_lines.index(right_line, i) + llo
                change_text = '\n'.join(right_lines[start_line:end_line])
                change = Change(start_line, end_line, llo, change_text, ADDLEFT)
                changes.append(change)






    
