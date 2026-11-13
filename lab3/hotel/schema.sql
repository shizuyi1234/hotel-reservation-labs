PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    salt TEXT NOT NULL,
    digest TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('guest','staff','admin')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS hotels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL COLLATE NOCASE,
    address TEXT NOT NULL,
    contact TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS hotels_city ON hotels(city);
CREATE TABLE IF NOT EXISTS grants (
    user_id INTEGER REFERENCES accounts(id),
    hotel_id INTEGER REFERENCES hotels(id),
    PRIMARY KEY(user_id, hotel_id)
);
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS rooms_hotel ON rooms(hotel_id);
CREATE TABLE IF NOT EXISTS inventory (
    room_id INTEGER REFERENCES rooms(id),
    day TEXT NOT NULL,
    total INTEGER NOT NULL CHECK(total >= 0),
    price INTEGER NOT NULL CHECK(price >= 0),
    stopped INTEGER NOT NULL CHECK(stopped IN (0,1)),
    PRIMARY KEY(room_id,day)
);
CREATE TABLE IF NOT EXISTS closures (
    id INTEGER PRIMARY KEY,
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    first_day TEXT NOT NULL,
    end_day TEXT NOT NULL CHECK(end_day > first_day),
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS closures_hotel ON closures(hotel_id,active);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES accounts(id),
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    hotel_name TEXT NOT NULL,
    room_name TEXT NOT NULL,
    arrival TEXT NOT NULL,
    departure TEXT NOT NULL CHECK(departure > arrival),
    people INTEGER NOT NULL CHECK(people > 0),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    total INTEGER NOT NULL CHECK(total >= 0),
    status TEXT NOT NULL CHECK(status IN
        ('pending','confirmed','staying','completed','cancelled','rejected','expired')),
    created_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    checked_in_at TEXT,
    checked_out_at TEXT,
    checkin_staff_id INTEGER REFERENCES accounts(id),
    checkout_staff_id INTEGER REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS orders_hotel ON orders(hotel_id,status,arrival);
CREATE INDEX IF NOT EXISTS orders_customer
    ON orders(customer_id,hotel_id,room_id,arrival,departure,status);
CREATE INDEX IF NOT EXISTS orders_due ON orders(status,deadline,departure);
CREATE TABLE IF NOT EXISTS nights (
    order_id INTEGER REFERENCES orders(id),
    revision INTEGER NOT NULL,
    room_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    price INTEGER NOT NULL CHECK(price >= 0),
    usage TEXT NOT NULL CHECK(usage IN ('reserved','used','released')),
    PRIMARY KEY(order_id,revision,day),
    FOREIGN KEY(room_id,day) REFERENCES inventory(room_id,day)
);
CREATE INDEX IF NOT EXISTS nights_stock ON nights(room_id,day,usage);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    actor_id INTEGER REFERENCES accounts(id),
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    before_status TEXT,
    after_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    room_id INTEGER NOT NULL REFERENCES rooms(id)
);
CREATE INDEX IF NOT EXISTS events_order ON events(order_id,id);
CREATE TABLE IF NOT EXISTS receipts (
    actor_id INTEGER REFERENCES accounts(id),
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY(actor_id,operation,request_id)
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    source TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    staff_id INTEGER REFERENCES accounts(id),
    contact_note TEXT,
    decision TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS case_open
    ON cases(order_id,source) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER REFERENCES accounts(id),
    hotel_id INTEGER REFERENCES hotels(id),
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    before_status TEXT,
    after_status TEXT,
    result TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_scope ON audit(hotel_id,at);
CREATE INDEX IF NOT EXISTS audit_time ON audit(at);
