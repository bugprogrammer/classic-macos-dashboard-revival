#!/bin/sh
# Dashboard 复活服务完整自检
# 用法：
#   sh test-dashboard.sh                 # 默认 http://127.0.0.1
#   sh test-dashboard.sh 192.168.0.15:8080
#   sh test-dashboard.sh http://192.168.0.15
set -u

HOST="${1:-127.0.0.1}"
case "$HOST" in
  http://*|https://*) B="$HOST" ;;
  *) B="http://$HOST" ;;
esac

TMP="$(mktemp -d 2>/dev/null || mktemp -d -t dash)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
warn() { WARN=$((WARN+1)); echo "  WARN  $1"; }

check() { # name file literal
  if grep -F -q "$3" "$2" 2>/dev/null; then ok "$1"; else bad "$1 (missing '$3')"; fi
}

get() { # path outfile
  code=$(curl -s --max-time 25 -o "$2" -w '%{http_code}' "$B$1") || code=000
  if [ "$code" != 200 ]; then bad "HTTP $code  GET $1"; return 1; fi
  return 0
}

post() { # path data outfile
  code=$(curl -s --max-time 25 -o "$3" -w '%{http_code}' -X POST \
         "$B$1" -H 'Content-Type: text/xml' --data-binary "$2") || code=000
  if [ "$code" != 200 ]; then bad "HTTP $code  POST $1"; return 1; fi
  return 0
}

echo "== target: $B =="

# 1 health
if get "/health" "$TMP/health"; then
  check "health" "$TMP/health" "Dashboard revival server OK"
fi

# 2 10.4/10.5 weather find city
if get "/adcbin/apple/Apple_find_city.asp?location=shanghai" "$TMP/find"; then
  check "10.4/10.5 find_city" "$TMP/find" "<location city="
  check "10.4/10.5 find_city postal" "$TMP/find" "postal="
fi

# 3 10.4/10.5 weather data
if get "/adcbin/apple/Apple_Weather_Data.asp?zipcode=om312222_1214581" "$TMP/wdata"; then
  check "10.4/10.5 weather CurrentConditions" "$TMP/wdata" "<CurrentConditions>"
  check "10.4/10.5 weather Temperature" "$TMP/wdata" "<Temperature>"
fi

# 4 10.6/10.7 weather location
if post "/dgw?apptype=weather" '<?xml version="1.0"?><request><query type="getlocationid"><phrase>shanghai</phrase></query></request>' "$TMP/loc"; then
  check "10.6/10.7 getlocationid city" "$TMP/loc" "<city>"
  check "10.6/10.7 getlocationid id" "$TMP/loc" "<id>"
fi

# 5 10.6/10.7 weather forecast
if post "/dgw?apptype=weather" '<?xml version="1.0"?><request><query type="getforecastbylocationid"><list><id>om312222_1214581</id></list></query></request>' "$TMP/fc"; then
  check "10.6/10.7 forecast condition" "$TMP/fc" "<condition "
  check "10.6/10.7 forecast day" "$TMP/fc" "<forecast "
fi

# 6 dgw quotes (10.7 full fields)
if post "/dgw?apptype=finance" '<?xml version="1.0"?><request><query type="getquotes"><list><symbol>AAPL</symbol><symbol>^DJI</symbol></list></query></request>' "$TMP/q"; then
  check "dgw getquotes quote" "$TMP/q" "<quote>"
  check "dgw getquotes name" "$TMP/q" "<name>"
  check "dgw getquotes timestamp" "$TMP/q" "<timestamp>"
  check "dgw getquotes marketcap" "$TMP/q" "<marketcap>"
fi

# 7 dgw getsymbol
if post "/dgw?apptype=finance" '<?xml version="1.0"?><request><query type="getsymbol"><phrase>NVDA</phrase></query></request>' "$TMP/sym"; then
  check "dgw getsymbol name" "$TMP/sym" "<name>"
  check "dgw getsymbol exchange" "$TMP/sym" "<exchange>"
fi

# 8 dgw getchart meta + point
if post "/dgw?apptype=finance" '<?xml version="1.0"?><request><query type="getchart"><symbol>AAPL</symbol><range>1m</range></query></request>' "$TMP/ch"; then
  check "dgw getchart meta" "$TMP/ch" "<meta>"
  check "dgw getchart marketopen" "$TMP/ch" "<marketopen>"
  check "dgw getchart point" "$TMP/ch" "<point "
fi

# 9 dgw convertcurrency USD
if post "/dgw?apptype=finance" '<?xml version="1.0"?><request><query type="convertcurrency"><from/><to/><amount/></query></request>' "$TMP/cv"; then
  check "dgw convertcurrency conversion" "$TMP/cv" "<conversion>"
  check "dgw convertcurrency USD" "$TMP/cv" "<to>USD</to>"
fi

# 10 translation
if post "/trans" 'id=sherlock&charset=utf-8&api=1&lp=en_zh&text=hello' "$TMP/tr"; then
  if grep -F -q "x!" "$TMP/tr"; then
    if grep -F -q "hello]" "$TMP/tr"; then
      warn "translation returned original (set BAIDU_APPID/BAIDU_KEY for real translation)"
    else
      ok "translation en->zh"
    fi
  else
    bad "translation (no x! prefix)"
  fi
fi

# 11 legacy quote.asp
if get "/fq/applewidgets/quote.asp?key=tHisIsApplewidgeTs&symbols=INDU,COMPX,AAPL" "$TMP/lq"; then
  check "10.4 quote INDEXSET" "$TMP/lq" "<INDEXSET>"
  check "10.4 quote LASTTRADE" "$TMP/lq" "<LASTTRADE>"
fi

# 12 legacy ticker.asp
if get "/fq/applewidgets/ticker.asp?key=tHisIsApplewidgeTs&search=NVDA" "$TMP/lt"; then
  check "10.4 ticker TICKER" "$TMP/lt" "<TICKER>"
  check "10.4 ticker COMPANYNAME" "$TMP/lt" "<COMPANYNAME>"
fi

# 13 legacy chart.asp 6:M
if get "/fq/applewidgets/chart.asp?key=tHisIsApplewidgeTs&symbols=INDU&interval=D&period=6:M" "$TMP/lc"; then
  n=$(grep -o '<DATETIME>' "$TMP/lc" 2>/dev/null | wc -l | tr -d ' ')
  [ "${n:-0}" -ge 100 ] && ok "10.4 chart points=$n" || bad "10.4 chart points=$n (<100)"
fi

# 14 legacy forex.asp
if get "/fq/applewidgets/forex.asp?key=tHisIsApplewidgeTs" "$TMP/lf"; then
  check "10.4 forex RATE" "$TMP/lf" "<RATE>"
  check "10.4 forex US_UNIT" "$TMP/lf" "<US_UNIT>"
fi

echo
echo "== PASS=$PASS FAIL=$FAIL WARN=$WARN =="
[ "$FAIL" -eq 0 ]
