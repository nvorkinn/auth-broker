-- Gives every log record the standard fields: service, host, level and _msg.
-- Runs on docker_logs (container stdout/stderr), the :9880 HTTP input and OTLP logs.

-- The :9880 HTTP input is tagged by URI path (/logs -> "logs"), the OTLP input's by /v1/logs.
local HTTP_DEFAULT_SERVICE = "auth-broker"
local OTLP_DEFAULT_SERVICE = "otlp"

local LEVELS = {
  trace = "trace", debug = "debug", info = "info", notice = "info", log = "info",
  warn = "warn", warning = "warn", error = "error", err = "error",
  crit = "fatal", critical = "fatal", fatal = "fatal", panic = "fatal",
}
local NUMERIC = { [10] = "trace", [20] = "debug", [30] = "info", [40] = "warn", [50] = "error", [60] = "fatal" }
local LEVEL_KEYS = { "level", "levelname", "severity", "log_level", "lvl" }

local function normalize_level(v)
  if type(v) == "number" then return NUMERIC[v] end
  if type(v) == "string" then return LEVELS[string.lower(v)] end
  return nil
end

local function level_from_text(line)
  local head = string.upper(string.sub(line, 1, 200))
  for _, p in ipairs({ { "FATAL", "fatal" }, { "PANIC", "fatal" }, { "ERROR", "error" }, { "WARN", "warn" },
                       { "INFO", "info" }, { "DEBUG", "debug" } }) do
    if string.find(head, "%f[%a]" .. p[1] .. "%f[%A]") then return p[2] end
  end
  return nil
end

local function non_empty(v)
  if type(v) == "string" and v ~= "" then return v end
  return nil
end

function normalize(tag, ts, r)
  local msg = non_empty(r["_msg"]) or non_empty(r["message"]) or non_empty(r["msg"])
    or non_empty(r["log"]) or non_empty(r["body"]) or ""
  msg = string.gsub(msg, "[\r\n]+$", "")
  local raw = non_empty(r["log"]) or msg

  local service
  if tag == "docker_logs" then
    local attrs = r["attrs"]
    service = type(attrs) == "table" and non_empty(attrs["tag"]) or nil
    if not service then
      local id = string.match(r["path"] or "", "/containers/(%x+)/")
      service = id and string.sub(id, 1, 12) or "unknown"
    end
  else
    service = non_empty(r["service"])
      or (string.find(tag, "v1", 1, true) and OTLP_DEFAULT_SERVICE or HTTP_DEFAULT_SERVICE)
  end

  local level
  for _, k in ipairs(LEVEL_KEYS) do
    level = normalize_level(r[k])
    if level then break end
  end
  level = level or level_from_text(raw) or "info"

  local host = non_empty(r["host"]) or non_empty(r["_fb_host"]) or "unknown"

  for _, k in ipairs({ "path", "attrs", "stream", "log", "message", "msg", "body",
                       "levelname", "severity", "log_level", "lvl", "_fb_host" }) do
    r[k] = nil
  end
  r["_msg"] = msg
  r["service"] = service
  r["host"] = host
  r["level"] = level
  return 1, ts, r
end
