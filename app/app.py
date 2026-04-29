from pyexpat.errors import messages

from flask import Flask, flash, jsonify, request, Response, redirect, url_for, session, abort, render_template
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user
from werkzeug.utils import secure_filename
from pandas import read_excel
import requests
import sqlite3
import re
import os
import config
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

Limiter = Limiter(app, key_func = get_remote_address)



UPLOAD_FOLDER = config.UPLOAD_FOLDER
ALLOWED_EXTENSIONS = config.ALLOWED_EXTENSIONS
CALL_BACK_TOKEN = config.CALL_BACK_TOKEN
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# config
app.config.update(
    SECRET_KEY = config.SECRET_KEY,
)

def allowed_file(filename):
    return '.' in filename and \
            filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# flask-login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

class User(UserMixin):

    def __init__(self, id):
        self.id = id

    def __repr__(self):
        return "%d" % (self.id)

user = User(0)

# some producted url
@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    """

    """
    if request.method == "POST":
    #check if the post request has the file part
        if 'file' not in request.files:
            flash('No file part')
            session["message"] = f"No file part"
            return redirect(request.url)
        file = request.files['file']
        # if user does not select file, browser also
        # submit an empty part without filename
        if file.filename == '':
            flash('No selected file')
            session["message"] = f"No selected part"
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            rows, failures = import_database_from_excel(file_path)
            session['message'] = f"Imported {rows} rows of serials and {failures} rows of failure"
            os.remove(file_path)
            return redirect("/")

    message = session.get('message', None)
    session['message'] = ''

    html_str = f'''
    <!doctype html>
    <title>Upload new File</title>
    <h1>Upload new File</h1>
    <h3>{message}</h3>
    <form method=post enctype=multipart/form-data>
        <input type=file name=file>
        <input type=submit value=Upload>
    </form>
    '''

    return render_template('index.html')

@app.route("/login", methods=["GET", "POST"])
@Limiter.limit("5 per minute")
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if password == config.password and username == config.username:
            login_user(user)
            return redirect("/")
        else:
            return abort(401)
    else:
        html_str = Response('''
        <form action="" method="post">
            <p><input type=text name=username>
            <p><input type=password name=password>
            <p><input type=submit value=Login>
        </form>
        ''')

        return render_template("login.html")

# somewhere to logout
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return Response("<p>You have been logged out</p>")

# handle login failed
@app.errorhandler(401)
def page_not_found(error):
    return Response("<p>Login failed</p>")

#callback to reload the user object
@login_manager.user_loader
def load_user(userid):
    return User(userid)

@app.route("/ok")
def health_check():
    retr = {"message": "ok"}
    return jsonify(retr), 200

@app.route(f"/{CALL_BACK_TOKEN}/process", methods=["post"])
def process():
    """this is a callback from Kavenegar. will get sender and message will check if it is valid, then answer back."""
    data = request.form
    sender = data["from"]
    message = normalize_string(data["message"])
    print(f"received {message}, from {sender}")

    answer = check_serial(message)

    send_sms(sender, answer)
    ret = {"message": "processed"}
    return jsonify(ret), 200

@app.route("/send_sms")
def send_sms(message, receptor):
    """this function will get a MSISDN and a message, then uses Kavenegar to send sms."""
    url = f"https://api.kavenegar.com/v1/[config.API_KEY]/sms/send.json"

    data = {"message": message,
            "receptor": receptor}

    res = requests.post(url, data)
    print(f"message *{message}* send. status code is {res.status_code}")

def check_serial(serial):
    """
    this function will get one serial number and return appropriate answer to thant,
    after consulting the db
    """
    conn = sqlite3.connect(config.DATABASE_FILE_PATH)
    cur = conn.cursor()

    query = f"SELECT * FROM serials WHERE invalid_serial == '{serial}';"
    results = cur.execute(query)
    if len(results.fetchall()) > 0:
        return "this serial is among failed ones"

    query = f"SELECT * FROM serials WHERE start_serial <= '{serial}' and end_serial >= '{serial}';"
    results = cur.execute(query)
    if len(results.fetchall()) == 1:
        return "I found your serial"

    return "it was not in the db"

def normalize_string(data, fixed_size = 30):
    from_persion_char = "۱۲۳۴۵۶۷۸۹۰"
    from_arabic_char = "۱۲۳۴۵۶۷۸۹۰" # Must change to Arabic numbers
    to_char = "1234567890"
    for i in range(len(to_char)):
        data = data.replace(from_persion_char[i], to_char[i])
        data = data.replace(from_arabic_char[i], to_char[i])

    data = data.upper()
    # remove any non-alphanumeric character
    data = re.sub(r"\W", "", data)

    all_alpha = ""
    all_digit = ""

    for c in data:
        if c.isalpha():
            all_alpha += c
        elif c.isdigit():
            all_digit += c

    missing_zeros = fixed_size - len(all_alpha) - len(all_digit)

    data = all_alpha + "0" + missing_zeros + all_digit

    return data

def import_database_from_excel(filepath):
    """gets an Excel file name and imports lookup date (data and failures) from it"""
    # df contains lookup data in the form of
    # Row    Reference Number    Description    Start Serial    End Serial   Data

    # our sqlite database will contain two tables: serials and invalids
    conn = sqlite3.connect(config.DATABASE_FILE_PATH)
    cur = conn.cursor()

    # remove the serials table if exists, then create the new one
    cur.execute("DROP TABLE IF EXISTS serials")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS serials (
        id INTEGER PRIMARY KEY,
        ref TEXT,
        desc TEXT,
        start_serial TEXT,
        end_serial TEXT,
        date DATE);
                """)

    df = read_excel(filepath, 0)
    serial_counter = 0
    for index, (line, ref, desc, start_serial, end_serial, data) in df.iterrows():
        start_serial = normalize_string(start_serial)
        end_serial = normalize_string(end_serial)

        qurey = f'INSERT INTO serials VALUES ("{line}", "{ref}", "{desc}", "{start_serial}", "{end_serial}", "{data}");'
        cur.execute(qurey)
        conn.commit()
        serial_counter += 1

    # remove the serials table if exists, then create the new one
    cur.execute("DROP TABLE IF EXISTS invalids")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invalids (
        invalid_serial TEXT PRIMARY KEY);
                """)

    df = read_excel(filepath, 1) #Sheet one contains failed serial numbers. only one column
    invalid_counter = 0
    for index, (failed_serial, ) in df.iterrows():
        qurey = f'INSERT INTO invalids VALUES ("{failed_serial}");'
        cur.execute(qurey)
        conn.commit()
        invalid_counter += 1

    conn.close()

    return (serial_counter, invalid_counter)

if __name__ == "__main__":
    app.run("0.0.0.0", 5000, debug=True)