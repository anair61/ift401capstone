from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo
import random

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler

app = Flask(__name__)

app.config["SQLALCHEMY_DATABASE_URI"] = "mysql+pymysql://root:rootpassword@localhost/proj_db"
app.config["SECRET_KEY"] = "your-secret-key-here"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

ARIZONA_TZ = ZoneInfo("America/Phoenix")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
bcrypt = Bcrypt(app)

scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()


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
    balance = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
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
    last_update = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Holding(db.Model):
    __tablename__ = "holdings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    stock_id = db.Column(db.Integer, db.ForeignKey("stocks.id"), nullable=False)
    shares = db.Column(db.Integer, default=0, nullable=False)
    avg_cost = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "stock_id", name="uniq_user_stock"),
    )


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    txn_type = db.Column(db.Enum("buy", "sell", "deposit", "withdraw", name="txn_type_enum"), nullable=False)
    status = db.Column(db.Enum("pending", "completed", "cancelled", name="txn_status_enum"), default="completed", nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    stock_id = db.Column(db.Integer, db.ForeignKey("stocks.id"), nullable=True)
    shares = db.Column(db.Integer, nullable=True)
    price = db.Column(db.Numeric(12, 2), nullable=True)
    notes = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    stock = db.relationship("Stock", foreign_keys=[stock_id])
    user = db.relationship("Users", foreign_keys=[user_id])


class MarketSettings(db.Model):
    __tablename__ = "market_settings"

    id = db.Column(db.Integer, primary_key=True)
    open_time = db.Column(db.String(5), default="09:30", nullable=False)
    close_time = db.Column(db.String(5), default="16:00", nullable=False)
    active_days = db.Column(db.String(50), default="Mon,Tue,Wed,Thu,Fri", nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MarketHoliday(db.Model):
    __tablename__ = "market_holidays"

    id = db.Column(db.Integer, primary_key=True)
    market_settings_id = db.Column(db.Integer, db.ForeignKey("market_settings.id"), nullable=False)
    day = db.Column(db.Date, unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

@scheduler.task('interval', id='update_prices_and_execute_orders', seconds=30)
def auto_update_prices():
    with app.app_context():
        if is_market_open(arizona_now()):
            # Update all stock prices
            stocks = Stock.query.all()
            for stock in stocks:
                update_stock_price(stock)
            
            # Execute pending orders
            pending = Transaction.query.filter_by(status="pending").all()
            for txn in pending:
                execute_pending_order(txn)
            
            db.session.commit()
            print(f"[{arizona_now().strftime('%H:%M:%S')}] Updated prices and executed pending orders")

@login_manager.user_loader
def load_user(user_id):
    return Users.query.get(int(user_id))


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


def get_market_settings():
    settings = MarketSettings.query.first()
    if settings:
        return settings

    settings = MarketSettings(open_time="09:30", close_time="16:00", active_days="Mon,Tue,Wed,Thu,Fri")
    db.session.add(settings)
    db.session.commit()
    return settings

def arizona_now():
    return datetime.now(ARIZONA_TZ)

def get_active_days_set(active_days):
    if not active_days:
        return set()
    return {day.strip() for day in active_days.split(",") if day.strip()}


def is_market_open(now=None):
    now = now or arizona_now()
    settings = get_market_settings()

    today_name = now.strftime("%a")
    active_days = get_active_days_set(settings.active_days)

    if today_name not in active_days:
        return False

    holiday = MarketHoliday.query.filter_by(market_settings_id=settings.id, day=now.date()).first()
    if holiday:
        return False

    open_t = datetime.strptime(settings.open_time, "%H:%M").time()
    close_t = datetime.strptime(settings.close_time, "%H:%M").time()

    return open_t <= now.time() <= close_t


def get_or_create_cash_account(user_id):
    cash_account = CashAccount.query.filter_by(user_id=user_id).first() 
    if cash_account:
        return cash_account

    cash_account = CashAccount(user_id=user_id, balance=Decimal("0.00"))
    db.session.add(cash_account)
    db.session.flush()
    return cash_account


def update_stock_price(stock):
    change_percent = Decimal(str(random.uniform(-0.02, 0.02)))
    new_price = stock.price * (Decimal("1.00") + change_percent)
    new_price = new_price.quantize(Decimal("0.01"))

    if new_price < Decimal("1.00"):
        new_price = Decimal("1.00")

    stock.price = new_price

    if new_price > stock.high_price:
        stock.high_price = new_price
    if new_price < stock.low_price:
        stock.low_price = new_price

    stock.last_update = datetime.utcnow() 


def execute_pending_order(txn):
    if txn.status != "pending":
        return

    stock = Stock.query.get(txn.stock_id)
    if not stock:
        return

    cash_account = get_or_create_cash_account(txn.user_id)
    holding = Holding.query.filter_by(user_id=txn.user_id, stock_id=txn.stock_id).first()

    if txn.txn_type == "buy":
        if cash_account.balance < txn.amount:
            return

        cash_account.balance -= txn.amount

        if not holding:
            holding = Holding(user_id=txn.user_id, stock_id=txn.stock_id, shares=0, avg_cost=Decimal("0.00"))
            db.session.add(holding)
            db.session.flush()

        old_total_cost = Decimal(holding.avg_cost) * holding.shares
        new_total_cost = old_total_cost + Decimal(txn.amount)
        new_total_shares = holding.shares + txn.shares

        holding.shares = new_total_shares
        holding.avg_cost = (new_total_cost / new_total_shares).quantize(Decimal("0.01"))

    elif txn.txn_type == "sell":
        if not holding or holding.shares < txn.shares:
            return

        holding.shares -= txn.shares
        cash_account.balance += txn.amount

        if holding.shares == 0:
            db.session.delete(holding)

    txn.status = "completed"
    txn.completed_at = datetime.utcnow() 



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

@app.route("/")
def index():
    return redirect(url_for("home"))

@app.route("/home")
@login_required
def home():
    market_now = arizona_now()
    settings = get_market_settings()

    if is_market_open(market_now):
        pending = Transaction.query.filter_by(user_id=current_user.id, status="pending").all()
        for txn in pending:
            execute_pending_order(txn)
        db.session.commit()

    # Check if user is admin
    if current_user.role == "admin":
        # Admin Dashboard Data
        total_users = Users.query.count()
        total_stocks = Stock.query.count()
        total_transactions = Transaction.query.count()
        pending_orders = Transaction.query.filter_by(status="pending").count()
        
        # Recent system transactions
        recent_transactions = Transaction.query.order_by(
            Transaction.created_at.desc()
        ).limit(10).all()

        return render_template(
            "home.html",
            market_open=is_market_open(market_now),
            market_now=market_now,
            settings=settings,
            is_admin=True,
            total_users=total_users,
            total_stocks=total_stocks,
            total_transactions=total_transactions,
            pending_orders=pending_orders,
            recent_transactions=recent_transactions,
        )
    
    # Regular User Dashboard Data
    cash_account = get_or_create_cash_account(current_user.id)
    cash_balance = cash_account.balance

    holdings = Holding.query.filter_by(user_id=current_user.id).all()
    portfolio_value = Decimal("0.00")
    for h in holdings:
        stock = Stock.query.get(h.stock_id)
        if stock:
            portfolio_value += Decimal(stock.price) * h.shares

    total_value = cash_balance + portfolio_value

    recent_transactions = Transaction.query.filter_by(
        user_id=current_user.id
    ).order_by(Transaction.created_at.desc()).limit(5).all()

    return render_template(
        "home.html",
        market_open=is_market_open(market_now),
        market_now=market_now,
        settings=settings,
        is_admin=False,
        cash_balance=cash_balance,
        portfolio_value=portfolio_value,
        total_value=total_value,
        recent_transactions=recent_transactions,
    )

@app.route("/stocks")
@login_required
def stocks():
    market_now = arizona_now()
    settings = get_market_settings()
    all_stocks = Stock.query.order_by(Stock.company_name.asc()).all()
    
    # Get cash balance for buying power display
    cash_account = get_or_create_cash_account(current_user.id)
    cash_balance = cash_account.balance

    if is_market_open(market_now):
        pending = Transaction.query.filter_by(user_id=current_user.id, status="pending").all()
        for txn in pending:
            execute_pending_order(txn)
        db.session.commit()

    return render_template(
        "stocks.html",
        market_open=is_market_open(market_now),
        market_now=market_now,
        settings=settings,
        stocks=all_stocks,
        cash_balance=cash_balance,
    )

@app.route("/transaction/<int:stock_id>", methods=["GET", "POST"])
@login_required
def stock_transaction(stock_id):
    stock = Stock.query.get_or_404(stock_id)
    cash_account = get_or_create_cash_account(current_user.id)
    holding = Holding.query.filter_by(user_id=current_user.id, stock_id=stock.id).first()
    owned = holding.shares if holding else 0
    market_now = arizona_now()
    market_open = is_market_open(market_now)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()

        try:
            shares = int(request.form.get("shares", "0"))
            # Use the locked price from the form instead of current stock price
            locked_price = Decimal(request.form.get("locked_price", "0"))
        except (ValueError, InvalidOperation):
            flash("Invalid transaction data.", "warning")
            return redirect(url_for("stock_transaction", stock_id=stock.id))

        if action not in {"buy", "sell"}:
            flash("Invalid transaction type.", "danger")
            return redirect(url_for("stock_transaction", stock_id=stock.id))

        if shares <= 0:
            flash("Shares must be greater than 0.", "warning")
            return redirect(url_for("stock_transaction", stock_id=stock.id))

        if locked_price <= Decimal("0.00"):
            flash("Invalid price.", "danger")
            return redirect(url_for("stock_transaction", stock_id=stock.id))

        price = locked_price
        total_amount = (price * shares).quantize(Decimal("0.01"))

        if action == "buy" and cash_account.balance < total_amount:
            flash("Insufficient cash balance.", "danger")
            return redirect(url_for("stock_transaction", stock_id=stock.id))

        if action == "sell" and owned < shares:
            flash("You do not own enough shares.", "danger")
            return redirect(url_for("stock_transaction", stock_id=stock.id)) 

        transaction = Transaction(
            user_id=current_user.id,
            txn_type=action,
            status="completed" if market_open else "pending",
            amount=total_amount,
            stock_id=stock.id,
            shares=shares,
            price=price,
            notes=f"{action.capitalize()} {shares} share(s) of {stock.ticker}",
            completed_at=datetime.utcnow() if market_open else None,
        )
        db.session.add(transaction)
        db.session.flush()

        if market_open:
            if action == "buy":
                if not holding:
                    holding = Holding(
                        user_id=current_user.id,
                        stock_id=stock.id,
                        shares=0,
                        avg_cost=Decimal("0.00"),
                    )
                    db.session.add(holding)
                    db.session.flush()

                old_total_cost = Decimal(holding.avg_cost) * holding.shares
                new_total_cost = old_total_cost + total_amount
                new_total_shares = holding.shares + shares

                holding.shares = new_total_shares
                holding.avg_cost = (new_total_cost / new_total_shares).quantize(Decimal("0.01"))
                cash_account.balance -= total_amount
            else:
                if holding:
                    holding.shares -= shares
                    if holding.shares == 0:
                        db.session.delete(holding) 
                cash_account.balance += total_amount

        db.session.commit()

        if market_open:
            flash(f"{action.capitalize()} completed successfully.", "success")
        else:
            flash(f"Market is closed. Your {action} order is pending.", "warning")
        return redirect(url_for("transactions"))

    return render_template(
        "transaction.html",
        stock=stock,
        cash_balance=cash_account.balance,
        owned=owned,
        market_open=market_open,
        market_now=market_now,
        settings=get_market_settings(),
        locked_price=stock.price,  # Pass the current price as locked price
    )

@app.route("/portfolio")
@login_required
def portfolio():
    # Execute pending orders if market is open
    if is_market_open(arizona_now()):
        pending = Transaction.query.filter_by(user_id=current_user.id, status="pending").all()
        for txn in pending:
            execute_pending_order(txn)
        db.session.commit()
    
    cash_account = get_or_create_cash_account(current_user.id)
    cash_balance = cash_account.balance
    holdings = Holding.query.filter_by(user_id=current_user.id).all()

    holdings_data = []
    portfolio_value = Decimal("0.00")

    for h in holdings:
        stock = Stock.query.get(h.stock_id)
        if not stock:
            continue

        current_value = Decimal(stock.price) * h.shares
        portfolio_value += current_value

        holdings_data.append({
            "ticker": stock.ticker,
            "company_name": stock.company_name,
            "shares": h.shares,
            "avg_cost": Decimal(h.avg_cost),
            "price": Decimal(stock.price),
            "value": current_value,
        })

    account_total = cash_balance + portfolio_value

    return render_template(
        "portfolio.html",
        cash_balance=cash_balance,
        holdings=holdings_data,
        portfolio_value=portfolio_value,
        account_total=account_total
    )


@app.route("/transactions")
@login_required
def transactions():
    if is_market_open(arizona_now()):
        pending = Transaction.query.filter_by(user_id=current_user.id, status="pending").all()
        for txn in pending:
            execute_pending_order(txn)
        db.session.commit()

    all_transactions = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.created_at.desc()).all()
    return render_template("transactions.html", transactions=all_transactions)

@app.route("/transaction/<int:transaction_id>/cancel", methods=["POST"])
@login_required
def cancel_transaction(transaction_id):
    txn = Transaction.query.get_or_404(transaction_id)

    if txn.user_id != current_user.id:
        abort(403)

    if txn.status != "pending":
        flash("Only pending orders can be cancelled.", "warning")
        return redirect(url_for("transactions"))
    
    txn.status = "cancelled"
    db.session.commit()

    flash("Order cancelled successfully.", "success")
    return redirect(url_for("transactions"))


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
            cash_account.balance += amount
        else:
            cash_account.balance -= amount

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

@app.route("/admin/market-hours", methods=["GET", "POST"])
@login_required
@admin_required
def market_hours():
    settings = get_market_settings()
    
    if request.method == "POST":
        open_time = (request.form.get("open_time") or "").strip()
        close_time = (request.form.get("close_time") or "").strip()
        
        try:
            if open_time:
                datetime.strptime(open_time, "%H:%M") 
                settings.open_time = open_time
            if close_time:
                datetime.strptime(close_time, "%H:%M")  
                settings.close_time = close_time
            
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
        cleaned = [d for d in selected_days if d in valid_days]

        settings.active_days = ",".join(cleaned)
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
def delete_holiday(holiday_id):
    holiday = MarketHoliday.query.get_or_404(holiday_id)
    db.session.delete(holiday)
    db.session.commit()

    flash("Holiday removed.", "success")
    return redirect(url_for("market_schedule"))

@app.route("/admin/stocks", methods=["GET", "POST"])
@login_required
@admin_required
def admin_stocks():
    if request.method == "POST":
        company_name = (request.form.get("company_name") or "").strip()
        ticker = (request.form.get("ticker") or "").strip().upper()

        try:
            price = Decimal(request.form.get("price", "0")).quantize(Decimal("0.01"))
            volume = int(request.form.get("volume", "0"))
        except (InvalidOperation, TypeError, ValueError):
            flash("Enter valid stock details.", "warning")
            return redirect(url_for("admin_stocks"))

        if not company_name or not ticker:
            flash("Company name and ticker are required.", "warning")
            return redirect(url_for("admin_stocks"))

        if price <= Decimal("0.00"):
            flash("Price must be greater than 0.", "warning")
            return redirect(url_for("admin_stocks"))

        if volume < 0:
            flash("Volume cannot be negative.", "warning")
            return redirect(url_for("admin_stocks"))

        existing_stock = Stock.query.filter_by(ticker=ticker).first()
        if existing_stock:
            flash("Ticker already exists.", "danger")
            return redirect(url_for("admin_stocks"))

        stock = Stock(
            company_name=company_name,
            ticker=ticker,
            price=price,
            volume=volume,
            open_price=price,
            high_price=price,
            low_price=price,
        )
        db.session.add(stock)
        db.session.commit()

        flash("Stock added successfully.", "success")
        return redirect(url_for("admin_stocks"))

    all_stocks = Stock.query.order_by(Stock.company_name.asc()).all()
    return render_template("admin_stocks.html", stocks=all_stocks)

if __name__ == "__main__":
    app.run(debug=False)
