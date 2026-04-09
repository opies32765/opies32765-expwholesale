CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    phone VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(100),
    company VARCHAR(100),
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bids (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    phone VARCHAR(20) NOT NULL,
    vin VARCHAR(17),
    year INTEGER,
    make VARCHAR(50),
    model VARCHAR(50),
    trim VARCHAR(100),
    mileage INTEGER,
    color VARCHAR(50),
    raw_message TEXT,
    status VARCHAR(20) DEFAULT 'new',
    bid_amount DECIMAL(10,2),
    bid_response TEXT,
    bid_sent_at TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bid_photos (
    id SERIAL PRIMARY KEY,
    bid_id INTEGER REFERENCES bids(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    vin_extracted VARCHAR(17),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bid_messages (
    id SERIAL PRIMARY KEY,
    bid_id INTEGER REFERENCES bids(id) ON DELETE CASCADE,
    direction VARCHAR(10) NOT NULL,
    message TEXT,
    from_phone VARCHAR(20),
    to_phone VARCHAR(20),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS valuations (
    id SERIAL PRIMARY KEY,
    bid_id INTEGER REFERENCES bids(id) ON DELETE CASCADE,
    source VARCHAR(50),
    data JSONB,
    fetched_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bids_status ON bids(status);
CREATE INDEX IF NOT EXISTS idx_bids_created ON bids(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bids_phone ON bids(phone);
CREATE INDEX IF NOT EXISTS idx_bids_vin ON bids(vin);
