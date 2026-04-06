from __future__ import annotations

from datetime import datetime, date, time

from flask import Flask, render_template, request, redirect, url_for, flash, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from functools import wraps
from decimal import Decimal, InvalidOperation

app = Flask(__name__)

# MySQL connection
app.config["SQLALCHEMY_DATABASE_URI"] = "mysql+pymysql://root:rootpassword@localhost/proj_db"
app.config["SECRET_KEY"] = "your-secret-key-here"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

bcrypt = Bcrypt(app)


# Models
class Users(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(200), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password = db.Column(db.String(250), nullable=False)
    role = db.Column(db.String(50), default="user", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CashAccount(db.Model):
    __tablename__ = "cash_accounts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    balance = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Stock(db.Model):
    __tablename__ = "stocks"

    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(200), nullable=False)
    ticker = db.Column(db.String(10), unique=True, nullable=False)
    price = db.Column(db.Numeric(12, 2), nullable=False)
    volume = db.Column(db.Integer, nullable=False)
    open_price = db.Column(db.Numeric(12, 2), nullable=False)
    high_price = db.Column(db.Numeric(12, 2), nullable=False)
    low_price = db.Column(db.Numeric(12, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Holding(db.Model):
    __tablename__ = "holdings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    stock_id = db.Column(db.Integer, db.ForeignKey("stocks.id"), nullable=False)

    shares = db.Column(db.Integer, default=0, nullable=False)
    avg_cost = db.Column(db.Numeric(12, 2), default=0, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "stock_id", name="uniq_user_stock"),
    )


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    stock_id = db.Column(db.Integer, db.ForeignKey("stocks.id"), nullable=False)

    side = db.Column(db.String(10), nullable=False)  # buy or sell
    shares = db.Column(db.Integer, nullable=False)

    price_at_submit = db.Column(db.Numeric(12, 2), nullable=False)

    status = db.Column(
        db.Enum("pending", "executed", "cancelled", name="order_status"),
        default="pending",
        nullable=False
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    executed_at = db.Column(db.DateTime, nullable=True)


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)

    txn_type = db.Column(
        db.Enum("buy", "sell", "deposit", "withdraw", name="txn_type_enum"),
        nullable=False
    )

    amount = db.Column(db.Numeric(12, 2), nullable=False)

    stock_id = db.Column(db.Integer, db.ForeignKey("stocks.id"), nullable=True)
    shares = db.Column(db.Integer, nullable=True)
    price = db.Column(db.Numeric(12, 2), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    stock = db.relationship("Stock", foreign_keys=[stock_id])
    user = db.relationship("Users", foreign_keys=[user_id])
    order = db.relationship("Order", foreign_keys=[order_id])

class MarketSettings(db.Model):
    __tablename__ = "market_settings"

    id = db.Column(db.Integer, primary_key=True)

    # validated using datetime.strptime
    open_time = db.Column(db.String(5), default="09:30", nullable=False)
    close_time = db.Column(db.String(5), default="16:00", nullable=False)

    # replaces 7 boolean columns
    # example: "Mon,Tue,Wed,Thu,Fri"
    active_days = db.Column(db.String(50), default="Mon,Tue,Wed,Thu,Fri", nullable=False)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MarketHoliday(db.Model):
    __tablename__ = "market_holidays"

    id = db.Column(db.Integer, primary_key=True)

    market_settings_id = db.Column(db.Integer, db.ForeignKey("market_settings.id"), nullable=False)

    day = db.Column(db.Date, unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


@login_manager.user_loader
def load_user(user_id: str):
    return Users.query.get(int(user_id))


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


def get_market_settings() -> MarketSettings:
    settings = MarketSettings.query.first()
    if settings:
        return settings

    settings = MarketSettings(
        open_time="09:30",
        close_time="16:00",
        active_days="Mon,Tue,Wed,Thu,Fri"
    )
    db.session.add(settings)
    db.session.commit()
    return settings


def validate_time_string(value: str) -> str:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.strftime("%H:%M")


def get_active_days_set(active_days: str):
    if not active_days:
        return set()
    return {day.strip() for day in active_days.split(",") if day.strip()}


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    settings = get_market_settings()

    today_name = now.strftime("%a")
    active_days = get_active_days_set(settings.active_days)

    if today_name not in active_days:
        return False

    holiday_exists = MarketHoliday.query.filter_by(
        market_settings_id=settings.id,
        day=now.date()
    ).first()

    if holiday_exists is not None:
        return False

    open_t = datetime.strptime(settings.open_time, "%H:%M").time()
    close_t = datetime.strptime(settings.close_time, "%H:%M").time()

    return open_t <= now.time() <= close_t

def get_or_create_cash_account(user_id: int) -> CashAccount:
    cash_account = CashAccount.query.filter_by(user_id=user_id).first()
    if cash_account:
        return cash_account

    cash_account = CashAccount(user_id=user_id, balance=Decimal("0.00"))
    db.session.add(cash_account)
    db.session.flush()
    return cash_account

# Auth
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not full_name or not username or not email or not password:
            flash("Please fill out all fields.", "warning")
            return redirect(url_for("register"))

        if Users.query.filter_by(username=username).first():
            flash("Username already exists.", "danger")
            return redirect(url_for("register"))

        if Users.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for("register"))

        hashed_password = bcrypt.generate_password_hash(password).decode("utf-8")

        user = Users(
            full_name=full_name,
            username=username,
            email=email,
            password=hashed_password,
            role="user",
        )
        db.session.add(user)
        db.session.commit()

        flash("Account created. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("sign_up.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = Users.query.filter_by(username=username).first()

        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            flash("Logged in successfully.", "success")
            return redirect(url_for("home"))

        flash("Invalid username or password.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# Home
@app.route("/")
@login_required
def home():
    return render_template("home.html")


# Placeholder user pages
@app.route("/stocks")
@login_required
def stocks():
    market_open = is_market_open()
    all_stocks = Stock.query.order_by(Stock.company_name.asc()).all()
    return render_template("stocks.html", market_open=market_open, stocks=all_stocks)

@app.route("/buy/<int:stock_id>", methods=["GET", "POST"])
@login_required
def buy_stock(stock_id):
    stock = Stock.query.get_or_404(stock_id)
    cash = CashAccount.query.filter_by(user_id=current_user.id).first()

    if request.method == "POST":
        shares = int(request.form.get("shares", 0))

        order = Order(
            user_id=current_user.id,
            stock_id=stock.id,
            side="buy",
            shares=shares,
            price_at_submit=stock.price,
            status="pending"
        )

        db.session.add(order)
        db.session.commit()

        flash("Buy order placed.", "success")
        return redirect(url_for("stocks"))

    return render_template("buy.html", stock=stock, cash_balance=cash.balance)

@app.route("/sell/<int:stock_id>", methods=["GET", "POST"])
@login_required
def sell_stock(stock_id):
    stock = Stock.query.get_or_404(stock_id)
    holding = Holding.query.filter_by(user_id=current_user.id, stock_id=stock.id).first()

    owned = holding.shares if holding else 0

    if request.method == "POST":
        try:
            shares = int(request.form.get("shares", 0))
        except (TypeError, ValueError):
            flash("Enter a valid number of shares.", "warning")
            return redirect(url_for("sell_stock", stock_id=stock.id))

        if shares <= 0:
            flash("Shares must be greater than 0.", "warning")
            return redirect(url_for("sell_stock", stock_id=stock.id))

        if not holding or holding.shares < shares:
            flash("Not enough shares to sell.", "danger")
            return redirect(url_for("sell_stock", stock_id=stock.id))

        total_value = Decimal(stock.price) * shares

        order = Order(
            user_id=current_user.id,
            stock_id=stock.id,
            side="sell",
            shares=shares,
            price_at_submit=stock.price,
            status="executed",
            executed_at=datetime.utcnow()
        )
        db.session.add(order)
        db.session.flush()

        holding.shares = holding.shares - shares
        if holding.shares == 0:
            db.session.delete(holding)

        cash = CashAccount.query.filter_by(user_id=current_user.id).first()
        if not cash:
            cash = CashAccount(user_id=current_user.id, balance=Decimal("0.00"))
            db.session.add(cash)
            db.session.flush()

        cash.balance = cash.balance + total_value

@app.route("/portfolio")
@login_required
def portfolio():
    cash_account = CashAccount.query.filter_by(user_id=current_user.id).first()
    cash_balance = cash_account.balance if cash_account else Decimal("0.00")

    holdings = Holding.query.filter_by(user_id=current_user.id).all()

    holdings_data = []
    total_value = Decimal("0.00")

    for h in holdings:
        stock = Stock.query.get(h.stock_id)
        current_value = Decimal(stock.price) * h.shares
        total_value += current_value

        holdings_data.append({
            "ticker": stock.ticker,
            "company": stock.company_name,
            "shares": h.shares,
            "avg_cost": h.avg_cost,
            "price": stock.price,
            "value": current_value
        })

    account_total = cash_balance + total_value

    return render_template(
        "portfolio.html",
        cash_balance=cash_balance,
        holdings=holdings_data,
        total_value=total_value,
        account_total=account_total
    )


@app.route("/transactions")
@login_required
def transactions():
    all_transactions = Transaction.query.filter_by(
        user_id=current_user.id
    ).order_by(Transaction.created_at.desc()).all()

    return render_template("transactions.html", transactions=all_transactions)


@app.route("/cash", methods=["GET", "POST"])
@login_required
def cash():
    cash_account = get_or_create_cash_account(current_user.id)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()

        try:
            amount = Decimal(request.form.get("amount", "0")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            flash("Enter a valid amount.", "warning")
            return redirect(url_for("cash"))

        if amount <= Decimal("0.00"):
            flash("Amount must be greater than 0.", "warning")
            return redirect(url_for("cash"))

        if action not in {"deposit", "withdraw"}:
            flash("Invalid cash action.", "danger")
            return redirect(url_for("cash"))

        if action == "withdraw" and cash_account.balance < amount:
            flash("Insufficient balance.", "danger")
            return redirect(url_for("cash"))

        if action == "deposit":
            cash_account.balance = cash_account.balance + amount
        else:
            cash_account.balance = cash_account.balance - amount

        txn = Transaction(
            user_id=current_user.id,
            txn_type=action,
            status="completed",
            amount=amount,
            stock_id=None,
            shares=None,
            price=None,
            notes=f"Cash {action}",
            completed_at=datetime.utcnow(),
        )
        db.session.add(txn)
        db.session.commit()

        flash(f"{action.capitalize()} successful.", "success")
        return redirect(url_for("cash"))

    return render_template("cash.html", cash_balance=cash_account.balance)

# Admin pages
def validate_time_string(value: str) -> str:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.strftime("%H:%M")


def get_active_days_set(active_days: str):
    if not active_days:
        return set()
    return {d.strip() for d in active_days.split(",")}


@app.route("/admin/market-hours", methods=["GET", "POST"])
@login_required
@admin_required
def market_hours():
    settings = get_market_settings()

    if request.method == "POST":
        open_time_val = (request.form.get("open_time") or "").strip()
        close_time_val = (request.form.get("close_time") or "").strip()

        try:
            if open_time_val:
                settings.open_time = validate_time_string(open_time_val)
            if close_time_val:
                settings.close_time = validate_time_string(close_time_val)

            db.session.commit()
            flash("Market hours updated.", "success")

        except ValueError:
            flash("Invalid time format. Use HH:MM.", "warning")

        return redirect(url_for("market_hours"))

    return render_template("admin_market_hours.html", settings=settings)


@app.route("/admin/market-schedule", methods=["GET", "POST"])
@login_required
@admin_required
def market_schedule():
    settings = get_market_settings()

    if request.method == "POST":
        selected_days = request.form.getlist("active_days")

        valid_days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        cleaned_days = [d for d in selected_days if d in valid_days]

        settings.active_days = ",".join(cleaned_days)
        db.session.commit()

        flash("Market schedule updated.", "success")
        return redirect(url_for("market_schedule"))

    holidays = MarketHoliday.query.filter_by(
        market_settings_id=settings.id
    ).order_by(MarketHoliday.day.asc()).all()

    selected_days = get_active_days_set(settings.active_days)

    return render_template(
        "admin_market_schedule.html",
        settings=settings,
        holidays=holidays,
        selected_days=selected_days
    )


@app.route("/admin/holidays/add", methods=["POST"])
@login_required
@admin_required
def add_holiday():
    settings = get_market_settings()

    day_str = (request.form.get("day") or "").strip()
    name = (request.form.get("name") or "").strip()

    try:
        day_val = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        flash("Enter a valid date in YYYY-MM-DD format.", "warning")
        return redirect(url_for("market_schedule"))

    existing = MarketHoliday.query.filter_by(
        market_settings_id=settings.id,
        day=day_val
    ).first()

    if existing:
        flash("Holiday already exists.", "warning")
        return redirect(url_for("market_schedule"))

    holiday = MarketHoliday(
        market_settings_id=settings.id,
        day=day_val,
        name=name or None
    )

    db.session.add(holiday)
    db.session.commit()

    flash("Holiday added.", "success")
    return redirect(url_for("market_schedule"))


@app.route("/admin/holidays/<int:holiday_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_holiday(holiday_id: int):
    holiday = MarketHoliday.query.get_or_404(holiday_id)

    db.session.delete(holiday)
    db.session.commit()

    flash("Holiday removed.", "success")
    return redirect(url_for("market_schedule"))

if __name__ == "__main__":
    app.run(debug=True)

