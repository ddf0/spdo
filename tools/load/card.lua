-- Карточка обращения: GET /tickets/{номер} со случайным номером из
-- диапазона 1..SPDO_MAX_TICKET (история, связи, решение).
package.path = "/scripts/?.lua;" .. package.path
local common = require("common")

local max_ticket = tonumber(os.getenv("SPDO_MAX_TICKET") or "5000")

function init(args)
  common.seed()
end

function request()
  return wrk.format("GET", "/tickets/" .. math.random(max_ticket), common.headers())
end

setup = common.setup
response = common.response
done = common.report
