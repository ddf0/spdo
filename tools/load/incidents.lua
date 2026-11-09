-- Массовые обращения: GET /incidents (Ф20) — попарное сходство свежих
-- обращений, самый тяжёлый по вычислениям экран оператора.
package.path = "/scripts/?.lua;" .. package.path
local common = require("common")

function request()
  return wrk.format("GET", "/incidents", common.headers())
end

setup = common.setup
response = common.response
done = common.report
