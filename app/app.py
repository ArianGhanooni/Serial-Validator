from pyexpat.errors import messages

from flask import Flask, flash, jsonify, request, Response, redirect, url_for, abort, render_template
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user, current_user
from werkzeug.utils import secure_filename
from pandas import read_excel
import requests
import MySQLdb
import time
import datetime
import re
import os
import config
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

Limiter = Limiter(key_func = get_remote_address)
Limiter.init_app(app)


MAX_FLASH = 10
UPLOAD_FOLDER = config.UPLOAD_FOLDER
ALLOWED_EXTENSIONS = config.ALLOWED_EXTENSIONS
CALL_BACK_TOKEN = config.CALL_BACK_TOKEN
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# config
app.config.update(
    SECRET_KEY = config.SECRET_KEY,
)

def get_db_connection():
    return MySQLdb.connect(host=config.MySQL_HOST,
                         user=config.MySQL_USERNAME,
                         passwd=config.MySQL_PASSWORD,
                         db=config.MySQL_DB_NAME)

def allowed_file(filename):
    return '.' in filename and \
            filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# flask-login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message_category = 'danger'

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
            flash('No file part', 'danger')
            return redirect(request.url)
        file = request.files['file']
        # if user does not select file, browser also
        # submit an empty part without filename
        if file.filename == '':
            flash('No selected file', "danger")
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            rows, failures = import_database_from_excel(file_path)
            flash(f"Imported {rows} rows of serials and {failures} rows of failure", "success")
            os.remove(file_path)
            return redirect("/")

    db = get_db_connection()
    cur = db.cursor()

    # Get last 1000 SMS
    cur.execute("SELECT * FROM Processed_SMS ORDER BY date DESC LIMIT 1000")
    all_sms = cur.fetchall()

    sms_A = []

    for sms in all_sms:
        status, sender, message, answer, date = sms
        sms_A.append({'status': status, 'sender': sender, 'message': message, 'answer': answer, 'date': date})

    # Collect some status for GUI
    cur.execute("SELECT count(*) FROM Processed_SMS WHERE status = 'OK'")
    num_ok = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM Processed_SMS WHERE status = 'Not-Found'")
    num_Not_Found = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM Processed_SMS WHERE status = 'Double'")
    num_Double = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM Processed_SMS WHERE status = 'Failure'")
    num_Failure = cur.fetchone()[0]

    return render_template('index.html', data = {'sms_A': sms_A,
                                                 'OK': num_ok, 'Failure': num_Failure, 'Not_Found': num_Not_Found, 'Double': num_Double})

@app.route("/login", methods=["GET", "POST"])
@Limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if password == config.password and username == config.username:
            login_user(user)
            return redirect("/")
        else:
            return abort(401)
    else:
        return render_template("login.html")

# somewhere to logout
@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "success")
    return redirect("/login")

# handle login failed
@app.errorhandler(401)
def page_not_found(error):
    flash("Login Problem", "danger")
    return redirect("/login")

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

    status, answer = check_serial(message)

    db = get_db_connection()
    cur = db.cursor()

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO Processed_SMS (status, sender, message, answer, date) VALUES (%s, %s, %s, %s, %s)",
                (status, sender, message, answer, now))

    db.commit()
    db.close()

    send_sms(sender, answer)
    ret = {"message": "processed"}
    return jsonify(ret), 200

@app.route("/send_sms")
def send_sms(receptor, message):
    """this function will get a MSISDN and a message, then uses Kavenegar to send sms."""
    url = f"https://api.kavenegar.com/v1/{config.API_KEY}/sms/send.json"

    data = {"message": message,
            "receptor": receptor}

    res = requests.post(url, data)
    print(f"message *{message}* send. status code is {res.status_code}")

def check_serial(serial):
    """
    this function will get one serial number and return appropriate answer to thant,
    after consulting the db
    """

    origin_serial = serial
    serial = normalize_string(serial)

    db = get_db_connection()
    cur = db.cursor()

    results = cur.execute("SELECT * FROM invalids WHERE invalid_serial = %s", (serial,))
    if results > 0:
        db.close()
        answer = f'''{origin_serial}
        Please Try Again, Your Serial Number Was Not Recognized.
        Like FA1234567
        Please Call Support
        '''
        return "Failure", answer

    results = cur.execute("SELECT * FROM serials WHERE start_serial <= %s and end_serial >= %s", (serial, serial))

    if results > 1:
        db.close()
        answer = f'''{origin_serial}
        This Serial Number is OK
        '''
        return "Double", answer

    if results == 1:
        ret = cur.fetchone()
        ref_number = ret[1]
        desc = ret[2]
        date = ret[5].date()

        db.close()
        answer = f'''{origin_serial}
        {ref_number}
        {desc}
        Hologram Date: {date}
        '''
        return "OK", answer

    db.close()

    answer = f'''{origin_serial}
        We Can't Find Your Serial Number
        Please Contact With Support
        '''
    return "Not-Found", answer

@app.route("/check_one_serial", methods=["POST"])
@login_required
def check_one_serial():
    serial_to_check = request.form["serial"]
    status, answer = check_serial(serial_to_check)
    flash(f"{status} - {answer}", 'info')

    return redirect("/")

def normalize_string(data, fixed_size = 30):
    from_persian_char = "۱۲۳۴۵۶۷۸۹۰"
    from_arabic_char = "۱۲۳۴۵۶۷۸۹۰" # Must change to Arabic numbers
    to_char = "1234567890"
    for i in range(len(to_char)):
        data = data.replace(from_persian_char[i], to_char[i])
        data = data.replace(from_arabic_char[i], to_char[i])

    data = data.upper()
    # remove any non-alphanumeric character
    data = re.sub(r"\W", "", data)

    all_alpha = ""
    all_digit = ""

    for this_character in data:
        if this_character.isalpha():
            all_alpha += this_character
        elif this_character.isdigit():
            all_digit += this_character

    missing_zeros = fixed_size - len(all_alpha) - len(all_digit)

    data = all_alpha + "0" + str(missing_zeros) + all_digit

    return data

def import_database_from_excel(filepath):
    """gets an Excel file name and imports lookup date (data and failures) from it"""
    # df contains lookup data in the form of
    # Row    Reference Number    Description    Start Serial    End Serial   Data

    #Open database
    db = get_db_connection()

    # our sqlite database will contain two tables: serials and invalids
    cur = db.cursor()

    # remove the serials table if exists, then create the new one
    try:
        cur.execute("DROP TABLE IF EXISTS serials;")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS serials (
            id INTEGER AUTO_INCREMENT PRIMARY KEY,
            ref VARCHAR(200),
            description VARCHAR(200),
            start_serial CHAR(30),
            end_serial CHAR(30),
            date DATETIME,
            INDEX(start_serial, end_serial));
                    """)
        db.commit()

    except:
        flash("Problem importing database", "danger")

    df = read_excel(filepath, 0)
    serial_counter = 1
    total_flashes = 0
    line_number = 1

    for index, (line, ref, description, start_serial, end_serial, data) in df.iterrows():
        line_number += 1
        try:
            start_serial = normalize_string(str(start_serial))
            end_serial = normalize_string(str(end_serial))

            cur.execute("INSERT INTO serials VALUES (%s, %s, %s, %s, %s, %s);", (
                     line, ref, description, start_serial, end_serial, data))
            db.commit()
            serial_counter += 1
        except:
            total_flashes += 1
            if total_flashes < MAX_FLASH:
                flash(f"Error inserting line {line_number} from serials sheet Serials", "danger")
            else:
                flash("Too many errors", "danger")

    # remove the serials table if exists, then create the new one
    try:
        cur.execute("DROP TABLE IF EXISTS invalids;")
        cur.execute("""
        CREATE TABLE invalids (
            invalid_serial CHAR(30), INDEX(invalid_serial));
                    """)
        db.commit()
    except:
        flash("Error dropping and creating invalid database", "danger")

    df = read_excel(filepath, 1) #Sheet one contains failed serial numbers. only one column
    invalid_counter = 1
    line_number = 1

    for index, (failed_serial, ) in df.iterrows():
        line_number += 1
        try:
            failed_serial = normalize_string(failed_serial)
            cur.execute('INSERT INTO invalids VALUES (%s)', (failed_serial,))
            db.commit()
            invalid_counter += 1
        except:
            total_flashes += 1
            if total_flashes < MAX_FLASH:
                flash(f"Error inserting line {line_number} from serials sheet Invalids", "danger")
            else:
                flash("Too many errors", "danger")

    db.close()

    return (serial_counter, invalid_counter)

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html')

if __name__ == "__main__":
    app.run("0.0.0.0", 5000, debug=True)