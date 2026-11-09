-- Реестр обращений: GET /tickets с фильтрами и полнотекстовым поиском (Ф11).
package.path = "/scripts/?.lua;" .. package.path
local common = require("common")

local paths = {
  "/tickets",
  "/tickets?status=new",
  "/tickets?q=%D0%BF%D1%80%D0%B8%D0%BD%D1%82%D0%B5%D1%80",
  "/tickets?q=vpn&status=closed",
  "/tickets?page=40",
  "/tickets?q=%D0%BF%D0%B0%D1%80%D0%BE%D0%BB%D1%8C+%D0%BF%D0%BE%D1%87%D1%82%D0%B0",
  "/tickets?status=in_progress&page=5",
  "/tickets?q=%D0%BD%D0%B5+%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%B5%D1%82",
}

local i = 0

function init(args)
  common.seed()
  i = math.random(#paths)
end

function request()
  i = i % #paths + 1
  return wrk.format("GET", paths[i], common.headers())
end

setup = common.setup
response = common.response
done = common.report
