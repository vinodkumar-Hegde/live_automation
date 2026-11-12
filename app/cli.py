import argparse
import re
import uuid
from .main import init_database, database, PH


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['create-admin'])
    parser.add_argument('--username', default='admin')
    parser.add_argument('--display-name', default='DocTutorials Admin')
    parser.add_argument('--email', required=True)
    args = parser.parse_args()
    username = args.username.lower().strip()
    if not re.fullmatch(r'[a-z0-9._-]{3,50}', username):
        parser.error('Username must be at least 3 characters')
    from getpass import getpass
    password = getpass('Admin password (12+ characters): ')
    if len(password) < 12:
        parser.error('Password needs at least 12 characters')
    init_database()
    with database() as con:
        con.execute('INSERT INTO users(id,username,display_name,email,zoom_user_id,password_hash,role) VALUES(?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex,username,args.display_name,args.email.lower().strip(),'',PH.hash(password),'admin'))
    print('Administrator created:', username)


if __name__ == '__main__': main()
