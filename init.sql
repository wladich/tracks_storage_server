CREATE TABLE geodata (
  id SERIAL PRIMARY KEY,
  data BYTEA
);

create unique index unique_geodata on geodata(md5(data));

CREATE TABLE trackview (
  id SERIAL PRIMARY KEY,
  data BYTEA,
  hash char(22)
);

create unique index unique_trackview_hash on trackview(hash);

CREATE TABLE read_log (
  id SERIAL PRIMARY KEY,
  ip_addr INET,
  time TIMESTAMP,
  trackview_id INTEGER
);

CREATE TABLE write_log (
  id SERIAL PRIMARY KEY,
  ip_addr INET,
  time TIMESTAMP,
  trackview_id INTEGER
);