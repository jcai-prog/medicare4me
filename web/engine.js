/* Medicare4Me cost engine: estimates a year of Part D out-of-pocket drug costs for one plan.
   Rules modeled (CY2027): plan deductible (up to $700) on tiers where it applies; plan copay or
   coinsurance (with min/max) in initial coverage; $0 after $2,400 out of pocket; covered insulin
   at most $35 per month supply and no deductible. Drugs not on the formulary are paid at full
   price and do not count toward the $2,400 cap. */
var M4M = (function () {
  var OOP_CAP = 2400, INSULIN_CAP = 35;
  // channel index in the CMS cost-sharing rows: 0 preferred retail, 1 standard retail, 2 preferred mail, 3 standard mail
  function pickChannel(rows, mail) {
    if (!rows) return null;
    var order = mail ? [2, 3, 0, 1] : [0, 1];
    for (var i = 0; i < order.length; i++) {
      var c = rows[order[i]];
      if (c && c[0] !== 0) return { ch: order[i], c: c };
    }
    return null;
  }
  function share(c, price) {
    // c = [type, amount, min, max]; type 1 copay, 2 coinsurance
    if (c[0] === 1) return price == null ? c[1] : Math.min(c[1], price);
    if (c[0] === 2) {
      if (price == null) return null;
      var v = c[1] * price;
      if (c[3] > 0) v = Math.min(v, c[3]);
      if (c[2] > 0) v = Math.max(v, Math.min(c[2], price));
      return v;
    }
    return null;
  }
  function formularyEntry(drug, fidx) {
    for (var i = 0; i < drug.f.length; i++) if (drug.f[i][0] === fidx) return drug.f[i];
    return null;
  }
  function unitCost(drug, contract) {
    if (drug.p && drug.p[contract] != null) return { u: drug.p[contract], src: "plan" };
    if (drug.pm != null) return { u: drug.pm, src: "national" };
    return { u: null, src: "none" };
  }
  /* plan: county-file plan record + {cps, ded}; picks: [{rx, qty, drug}]; opts: {mail:bool}
     returns {total, byDrug:[{rx, covered, tier, flags, cost, full, unknown, priceSrc}], notes} */
  function estimate(plan, picks, opts) {
    var mail = !!(opts && opts.mail);
    var contract = plan.cps.split("_")[0];
    var res = { total: 0, byDrug: [], unknown: false, notCovered: 0, oop: 0, capHit: null };
    if (!picks.length) return res;
    var fillsPerYear = mail ? 4 : 12, daysCode = mail ? "2" : "1", mult = mail ? 3 : 1;
    var dedLeft = plan.pd ? Math.max(0, plan.ded || 0) : 0;
    var oop = 0;
    var per = picks.map(function (p) {
      var d = p.drug, fe = plan.pd ? formularyEntry(d, plan.f) : null, uc = unitCost(d, contract);
      return { rx: p.rx, d: d, fe: fe, price: uc.u == null ? null : uc.u * p.qty * mult, priceSrc: uc.src,
               covered: !!fe, tier: fe ? fe[1] : null, flags: fe ? fe[2] : 0, cost: 0, full: 0, unknown: false };
    });
    for (var fill = 0; fill < fillsPerYear; fill++) {
      for (var i = 0; i < per.length; i++) {
        var x = per[i], price = x.price;
        if (price != null) x.full += price;
        if (!x.covered) {
          if (price == null) { x.unknown = true; continue; }
          x.cost += price; res.notCovered += price; continue;
        }
        if (oop >= OOP_CAP) continue;
        var t = plan.cs && plan.cs[String(x.tier)];
        if (!t) { x.unknown = true; continue; }
        var pay = null;
        var isIns = !!x.d.ins;
        if (isIns) {
          var ins = plan.ins && plan.ins[x.tier + "_" + daysCode], v = null;
          if (ins) {
            var chs = mail ? [2, 3, 0, 1] : [0, 1];
            for (var k = 0; k < chs.length && v == null; k++) {
              if (ins[0][chs[k]] != null) v = ins[0][chs[k]];
              else if (ins[1][chs[k]] != null && price != null) v = ins[1][chs[k]] * price;
            }
          }
          if (v == null) {
            var pc = pickChannel(t["1_" + daysCode] || t["1_1"], mail);
            v = pc ? share(pc.c, price) : null;
          }
          if (v == null) v = INSULIN_CAP * mult;
          pay = Math.min(v, INSULIN_CAP * mult);
          if (price != null) pay = Math.min(pay, price);
        } else if (t.da && dedLeft > 0) {
          if (price == null) { x.unknown = true; continue; }
          var dpart = Math.min(price, dedLeft);
          dedLeft -= dpart;
          pay = dpart;
          var rest = price - dpart;
          if (rest > 0) {
            var pc2 = pickChannel(t["1_" + daysCode] || t["1_1"], mail);
            var s2 = pc2 ? share(pc2.c, rest) : null;
            pay += s2 == null ? rest : s2;
          }
        } else {
          var lvl = (!t.da && dedLeft > 0 && t["0_" + daysCode]) ? "0_" : "1_";
          var pc3 = pickChannel(t[lvl + daysCode], mail) || pickChannel(t[lvl + "1"], mail);
          if (!pc3) { x.unknown = true; continue; }
          if (!t[lvl + daysCode] && mail) { /* no 90-day row: treat as three 30-day fills */
            var one = share(pc3.c, price == null ? null : price / 3);
            pay = one == null ? null : one * 3;
          } else pay = share(pc3.c, price);
          if (pay == null) { x.unknown = true; continue; }
        }
        if (oop + pay >= OOP_CAP) { pay = OOP_CAP - oop; if (res.capHit == null) res.capHit = mail ? fill * 3 + 1 : fill + 1; }
        oop += pay; x.cost += pay;
      }
    }
    res.oop = oop;
    per.forEach(function (x) {
      res.total += x.cost;
      if (x.unknown) res.unknown = true;
      res.byDrug.push({ rx: x.rx, covered: x.covered, tier: x.tier, flags: x.flags, cost: x.cost, full: x.full,
                        unknown: x.unknown, priceSrc: x.priceSrc });
    });
    return res;
  }
  return { estimate: estimate, OOP_CAP: OOP_CAP };
})();
if (typeof module !== "undefined") module.exports = M4M;
