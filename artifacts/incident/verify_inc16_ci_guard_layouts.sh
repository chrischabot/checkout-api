#!/bin/sh
# Verifies the INC-16 CI guard condition (extracted verbatim from ci.yml) resolves
# the sibling fleet under BOTH supported layouts, and honestly skips only when the
# fleet is genuinely absent. A guard that fires under only ONE naming scheme is
# itself layout-dependent -- the exact defect INC-16 fixes.
guard() {
  # $1 = fleet root containing the sibling repos (the CI step runs from inside
  # checkout-api, so '..' is the fleet root; we parameterize it for testing).
  r="$1"
  if { [ -f "$r/fabric-gateway-demo/service/usage_aggregator.py" ] \
       || [ -f "$r/gateway/service/usage_aggregator.py" ]; } \
     && { [ -f "$r/fabric-ic-incident-target/checkout.py" ] \
          || [ -f "$r/incident-target/checkout.py" ]; }; then
    echo RUN
  else
    echo SKIP
  fi
}

fail=0
check() {
  desc="$1"; want="$2"; got="$3"
  if [ "$want" = "$got" ]; then
    printf 'PASS  %-46s expected=%-4s got=%s\n' "$desc" "$want" "$got"
  else
    printf 'FAIL  %-46s expected=%-4s got=%s\n' "$desc" "$want" "$got"
    fail=1
  fi
}

root=$(mktemp -d)

# 1. REAL names
mkdir -p "$root/real/fabric-gateway-demo/service" "$root/real/fabric-ic-incident-target"
touch "$root/real/fabric-gateway-demo/service/usage_aggregator.py" \
      "$root/real/fabric-ic-incident-target/checkout.py"
check "real repo names resolve"            RUN  "$(guard "$root/real")"

# 2. LEGACY names -- this is the case the original guard wrongly SKIPPED
mkdir -p "$root/legacy/gateway/service" "$root/legacy/incident-target"
touch "$root/legacy/gateway/service/usage_aggregator.py" \
      "$root/legacy/incident-target/checkout.py"
check "legacy repo names resolve (was SKIP)" RUN "$(guard "$root/legacy")"

# 3. MIXED layout (permutation 1) -- real gateway, legacy target
mkdir -p "$root/mixed1/fabric-gateway-demo/service" "$root/mixed1/incident-target"
touch "$root/mixed1/fabric-gateway-demo/service/usage_aggregator.py" \
      "$root/mixed1/incident-target/checkout.py"
check "mixed: real gateway + legacy target" RUN  "$(guard "$root/mixed1")"

# 3b. MIXED layout (permutation 2) -- legacy gateway, real target. The mirror case:
# the two repos are resolved by INDEPENDENT conditions, so both permutations must be
# exercised. Covering only one would leave half the mixed space unproven.
mkdir -p "$root/mixed2/gateway/service" "$root/mixed2/fabric-ic-incident-target"
touch "$root/mixed2/gateway/service/usage_aggregator.py" \
      "$root/mixed2/fabric-ic-incident-target/checkout.py"
check "mixed: legacy gateway + real target" RUN  "$(guard "$root/mixed2")"

# 4. NEGATIVE CONTROL -- bare CI checkout, no siblings at all: must SKIP, not RUN.
mkdir -p "$root/bare"
check "bare checkout honestly skips"       SKIP "$(guard "$root/bare")"

# 5. NEGATIVE CONTROL -- only the gateway present: the gate needs both, so SKIP.
mkdir -p "$root/half/gateway/service"
touch "$root/half/gateway/service/usage_aggregator.py"
check "half fleet (gateway only) skips"    SKIP "$(guard "$root/half")"

# 5b. NEGATIVE CONTROL -- only the target present. The mirror of the above: an AND
# that degenerated into an OR would pass 5 and fail here, so both halves are tested.
mkdir -p "$root/half2/fabric-ic-incident-target"
touch "$root/half2/fabric-ic-incident-target/checkout.py"
check "half fleet (target only) skips"     SKIP "$(guard "$root/half2")"

rm -rf "$root"
echo
if [ $fail -eq 0 ]; then
  echo "########## INC-16 CI GUARD: LAYOUT-INDEPENDENT (7/7) ##########"
else
  echo "########## INC-16 CI GUARD: FAILURES PRESENT ##########"
fi
exit $fail
