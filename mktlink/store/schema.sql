-- Схема mktlink. Один файл SQLite: при 3 запросах в минуту Postgres и Redis
-- были бы инфраструктурой ради инфраструктуры.
--
-- Деньги защищены ТРИГГЕРАМИ, а не дисциплиной вызывающего. Причина прямая:
-- потолок, живущий в коде приложения, обходится следующим написанным кодом,
-- а потолок в BEGIN IMMEDIATE-транзакции БД — нет.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================ прокси и деньги

CREATE TABLE IF NOT EXISTS proxy (
  p6_id        INTEGER PRIMARY KEY,          -- id у proxy6, он же наш
  ip           TEXT    NOT NULL,
  host         TEXT    NOT NULL,
  port         INTEGER NOT NULL,
  user         TEXT    NOT NULL,
  pass         TEXT    NOT NULL,
  version      INTEGER NOT NULL CHECK (version IN (3, 4)),
  descr        TEXT    NOT NULL,             -- последний ЖИВОЙ descr от getproxy
  state        TEXT    NOT NULL CHECK (state IN (
                 'ordered','unverified','validating','active',
                 'suspect','retired','expired','foreign')),
  -- Локальная половина двустороннего veto. Вторая половина — RENEW в descr.
  -- Любая из двух в одиночку запрещает продление.
  never_renew  INTEGER NOT NULL DEFAULT 0 CHECK (never_renew IN (0, 1)),
  -- Прокси, найденный с RENEW='R' и БЕЗ локальной строки, импортируется как
  -- never_renew=1 + adopt_pending=1. Инверсия дефолта намеренная: после
  -- полной потери локального стора мы обязаны молчать, а не благословлять.
  adopt_pending INTEGER NOT NULL DEFAULT 0 CHECK (adopt_pending IN (0, 1)),
  term_end     INTEGER NOT NULL,             -- unixtime_end от proxy6
  delete_after INTEGER,                      -- для legacy с включённым auto_prolong
  state_since  INTEGER NOT NULL DEFAULT (unixepoch()),
  created_at   INTEGER NOT NULL DEFAULT (unixepoch()),
  CHECK (adopt_pending = 0 OR never_renew = 1)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_proxy_state ON proxy(state);
CREATE INDEX IF NOT EXISTS idx_proxy_term ON proxy(term_end);

-- Отдельная таблица, а не колонка: запись обязана пережить любую ошибку в
-- proxy, включая её пересоздание. Это единственный источник истины «никогда».
CREATE TABLE IF NOT EXISTS never_prolong (
  p6_id      INTEGER PRIMARY KEY,
  reason     TEXT    NOT NULL,
  decided_at INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

CREATE TABLE IF NOT EXISTS proxy_order (
  nonce       TEXT    PRIMARY KEY,           -- mp1.ord.YYMMDD.XXXXXX
  version     INTEGER NOT NULL CHECK (version IN (3, 4)),
  period_days INTEGER NOT NULL CHECK (period_days > 0),
  status      TEXT    NOT NULL CHECK (status IN ('intent','landed','orphan','void')),
  p6_id       INTEGER,
  created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  settled_at  INTEGER
) STRICT;

CREATE TABLE IF NOT EXISTS proxy_renewal (
  id              INTEGER PRIMARY KEY,
  p6_id           INTEGER NOT NULL,
  term_end_before INTEGER NOT NULL,
  period_days     INTEGER NOT NULL CHECK (period_days > 0),
  status          TEXT    NOT NULL CHECK (status IN ('intent','confirmed','failed')),
  created_at      INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

-- Дубль продления отвергается самой БД, а не нашей логикой: term_end до
-- операции служит свидетелем, поэтому повтор при потерянном ответе не платит
-- второй раз.
CREATE UNIQUE INDEX IF NOT EXISTS uq_renewal_witness
  ON proxy_renewal(p6_id, term_end_before) WHERE status <> 'failed';

CREATE TABLE IF NOT EXISTS proxy_spend (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL DEFAULT (unixepoch()),  -- приложение ts НЕ передаёт
  kind        TEXT    NOT NULL CHECK (kind IN ('buy','prolong','forfeit','discard')),
  status      TEXT    NOT NULL CHECK (status IN ('intent','confirmed','void')),
  p6_id       INTEGER,
  nonce       TEXT,
  kop         INTEGER NOT NULL CHECK (kop >= 0),
  version     INTEGER NOT NULL CHECK (version IN (3, 4)),
  period_days INTEGER NOT NULL CHECK (period_days > 0),
  -- PRICE_MAX_DAY: 1.40 ₽/сут для v3, 4.00 для v4. Ловит опечатку в цене до
  -- того, как она станет тратой.
  CHECK (kop <= period_days * (CASE version WHEN 3 THEN 140 ELSE 400 END))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_spend_ts ON proxy_spend(ts);

-- ГЛАВНОЕ ПРО ПОТОЛКИ: они видят ТОЛЬКО деньги, которые мы решаем потратить.
-- 'forfeit' и 'discard' — учёт уже потраченного (часть строки 'buy'), их
-- суммирование было бы двойным счётом. И, что важнее, их вставка НЕ ИМЕЕТ
-- ПРАВА откатить транзакцию, которая останавливает списание: иначе денежный
-- потолок уничтожал бы запись never_renew, ради которой он и существует.
CREATE TRIGGER IF NOT EXISTS proxy_spend_caps_ins BEFORE INSERT ON proxy_spend
WHEN NEW.kind IN ('buy','prolong') BEGIN
  SELECT RAISE(ABORT,'TS_SKEW') WHERE abs(NEW.ts - unixepoch()) > 5;
  SELECT RAISE(ABORT,'MIN_BUY_GAP') WHERE NEW.kind='buy' AND
    (SELECT COALESCE(MAX(ts),0) FROM proxy_spend WHERE kind='buy' AND status<>'void')
      > unixepoch()-900;
  SELECT RAISE(ABORT,'CAP_BUYS_HOURLY') WHERE NEW.kind='buy' AND
    (SELECT COUNT(*) FROM proxy_spend
       WHERE kind='buy' AND status<>'void' AND ts>unixepoch()-3600) >= 3;
  SELECT RAISE(ABORT,'CAP_BUYS_DAILY') WHERE NEW.kind='buy' AND
    (SELECT COUNT(*) FROM proxy_spend
       WHERE kind='buy' AND status<>'void' AND ts>unixepoch()-86400) >= 6;
  SELECT RAISE(ABORT,'CAP_BUYS_MONTHLY') WHERE NEW.kind='buy' AND
    (SELECT COUNT(*) FROM proxy_spend
       WHERE kind='buy' AND status<>'void' AND ts>unixepoch()-2592000) >= 40;
  SELECT RAISE(ABORT,'CAP_RUB_MONTHLY') WHERE
    (SELECT COALESCE(SUM(kop),0) FROM proxy_spend
       WHERE kind IN ('buy','prolong') AND status<>'void' AND ts>unixepoch()-2592000)
      + NEW.kop > 60000;
END;

-- Та же защита на повышающем UPDATE: подтверждение закупки по реальной
-- котировке не должно проносить трату мимо потолка. Тоже kind-scoped.
CREATE TRIGGER IF NOT EXISTS proxy_spend_caps_upd BEFORE UPDATE OF kop ON proxy_spend
WHEN NEW.kop > OLD.kop AND NEW.kind IN ('buy','prolong') BEGIN
  SELECT RAISE(ABORT,'CAP_RUB_MONTHLY') WHERE
    (SELECT COALESCE(SUM(kop),0) FROM proxy_spend
       WHERE kind IN ('buy','prolong') AND status<>'void'
         AND ts>unixepoch()-2592000 AND id<>NEW.id) + NEW.kop > 60000;
END;

-- ============================================================ здоровье и лейны

-- Здоровье считается по ПАРЕ (прокси, маркетплейс), и это не педантизм:
-- у Ozon с датацентрового IP ожидается высокая доля молчаливых отказов, и
-- при учёте «по прокси целиком» он сжёг бы каждый адрес, включая те, что
-- Я.Маркет обслуживает прекрасно.
CREATE TABLE IF NOT EXISTS proxy_health (
  p6_id      INTEGER NOT NULL,
  mp         TEXT    NOT NULL CHECK (mp IN ('ozon','wb','ym')),
  ok_n       INTEGER NOT NULL DEFAULT 0,
  bad_n      INTEGER NOT NULL DEFAULT 0,
  captcha_n  INTEGER NOT NULL DEFAULT 0,
  last_ok    INTEGER,
  last_bad   INTEGER,
  PRIMARY KEY (p6_id, mp)
) STRICT;

CREATE TABLE IF NOT EXISTS proxy_attempt (
  id       INTEGER PRIMARY KEY,
  ts       INTEGER NOT NULL DEFAULT (unixepoch()),
  p6_id    INTEGER,
  mp       TEXT    NOT NULL,
  verdict  TEXT    NOT NULL,
  -- Атрибутация егресса: наблюдение, сделанное НЕ через прокси, не может
  -- быть уликой против прокси. Скорер читает только 'proxy'.
  egress   TEXT    NOT NULL CHECK (egress IN ('proxy','direct','forge')),
  latency_ms INTEGER
) STRICT;

CREATE INDEX IF NOT EXISTS idx_attempt_ts ON proxy_attempt(ts);

CREATE TABLE IF NOT EXISTS marketplace_policy (
  mp             TEXT PRIMARY KEY CHECK (mp IN ('ozon','wb','ym')),
  pool_policy    TEXT NOT NULL DEFAULT 'normal',
  version        INTEGER NOT NULL DEFAULT 3 CHECK (version IN (3, 4)),
  terminal_rung  INTEGER NOT NULL DEFAULT 0 CHECK (terminal_rung IN (0, 1)),
  updated_at     INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

-- Чужие прокси: считаем, печатаем в отчёте бутстрапа, НИКОГДА не трогаем.
CREATE TABLE IF NOT EXISTS foreign_ack (
  p6_id      INTEGER PRIMARY KEY,
  descr      TEXT    NOT NULL,
  seen_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  acked_by   TEXT
) STRICT;

-- ============================================================ сессии и данные

CREATE TABLE IF NOT EXISTS jar (
  mp            TEXT    NOT NULL CHECK (mp IN ('ozon','wb','ym')),
  proxy_id      INTEGER NOT NULL,
  cookie_header TEXT    NOT NULL,   -- yandex_gid уже перезаписан на 213
  ua            TEXT    NOT NULL,   -- НАБЛЮДЁННЫЙ при минтинге: Camoufox
                                    -- ротирует отпечаток на каждый запуск,
                                    -- поэтому замороженная константа тут врёт
  ff_major      INTEGER NOT NULL,   -- пинится ТОЛЬКО мажор Firefox
  minted_at     INTEGER NOT NULL,
  verified_at   INTEGER,            -- NULL = НЕ ОПУБЛИКОВАН, читателем не берётся
  PRIMARY KEY (mp, proxy_id)
) STRICT;

-- Межпроцессный гард спейсинга. Переживает рестарт, чтобы не выстрелить
-- дважды подряд после перезапуска.
CREATE TABLE IF NOT EXISTS spacing (
  mp                TEXT    NOT NULL,
  proxy_id          INTEGER NOT NULL,
  next_allowed_ms   INTEGER NOT NULL,
  PRIMARY KEY (mp, proxy_id)
) STRICT;

-- Запиненные пути к полям. Селектор только в эфемерном хранилище означал бы
-- 100 % SCHEMA_DRIFT после любой очистки: переименование ключа маркетплейсом
-- должно чиниться пушем конфига, а не деплоем.
CREATE TABLE IF NOT EXISTS selectors (
  mp         TEXT NOT NULL,
  name       TEXT NOT NULL,
  path       TEXT NOT NULL,
  pinned_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  PRIMARY KEY (mp, name)
) STRICT;

-- Пол для ответов stale: при холодном in-proc кэше это превращает 202 в
-- 200 status="stale" за единицы миллисекунд.
CREATE TABLE IF NOT EXISTS product (
  cache_key   TEXT PRIMARY KEY,
  mp          TEXT NOT NULL,
  canonical   TEXT NOT NULL,
  name        TEXT,
  seller_name TEXT,
  seller_id   TEXT,
  seller_status TEXT NOT NULL,
  payload     TEXT,
  fetched_at  INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

-- Короткие ссылки неизменяемы, поэтому кэшируются надолго: раскрутка стоит
-- до трёх хопов, и платить за неё дважды незачем.
CREATE TABLE IF NOT EXISTS shortlink (
  short_sha256 TEXT PRIMARY KEY,
  canonical    TEXT NOT NULL,
  hops         INTEGER NOT NULL,
  resolved_at  INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

CREATE TABLE IF NOT EXISTS api_key (
  key_id      TEXT PRIMARY KEY,
  hash        TEXT NOT NULL,
  daily_quota INTEGER NOT NULL DEFAULT 2000,
  disabled    INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
  created_at  INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

CREATE TABLE IF NOT EXISTS key_usage (
  key_id  TEXT NOT NULL,
  day     TEXT NOT NULL,
  credits INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key_id, day)
) STRICT;

-- request_log нужен, чтобы ДОКАЗАТЬ бюджет реальным p99, а не утверждать его.
CREATE TABLE IF NOT EXISTS request_log (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL DEFAULT (unixepoch()),
  trace_id    TEXT    NOT NULL,
  mp          TEXT,
  status      INTEGER NOT NULL,
  verdict     TEXT,
  elapsed_ms  INTEGER NOT NULL,
  budget_ms   INTEGER NOT NULL,
  ledger      TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts);

CREATE TABLE IF NOT EXISTS rejections (
  id     INTEGER PRIMARY KEY,
  ts     INTEGER NOT NULL DEFAULT (unixepoch()),
  code   TEXT    NOT NULL,
  host   TEXT,
  detail TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_rejections_ts ON rejections(ts);
