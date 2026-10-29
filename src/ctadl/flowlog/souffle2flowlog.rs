// Rust support for the programs that souffle2flowlog.py translates: Souffle's
// semantics for the builtins whose FlowLog counterparts differ, the checks for
// Souffle's runtime errors, the identities that keep FlowLog's integers on 64
// bits, and the helpers of the translator's "min-choice" encoding of Souffle's
// `choice-domain`. flowlog-compiler takes it as the program's UDF file
// (--udf-file), followed by the program's own functors, if it has any (CTADL's
// are in ctadl/souffle-logic/functors.rs).
//
// FlowLog passes `string` parameters as owned `String`s and integers as the
// matching Rust type. The translation keeps Souffle's `number` on 64 bits, as
// `int64` (`i64`). The choice helpers' internal ids are `int32`.
#![allow(non_snake_case, dead_code)]

use std::collections::HashMap;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::sync::{LazyLock, Mutex};

// ---------------------------------------------------------------------------
// Souffle 2.5 builtins whose FlowLog counterparts differ; souffle2flowlog.py
// rewrites `strlen`, `substr`, `to_number`, `^`, `ord`, `/` and `%` to these:
//   strlen, substr  Souffle counts bytes, FlowLog chars.
//   to_number       Souffle infers the base from a 0b/0x prefix and parses
//                   the longest valid prefix; FlowLog parses the whole text
//                   as decimal.
//   ^               Souffle truncates std::pow, so 1 ^ -1 = 1; FlowLog gives
//                   0 for every negative exponent.
//   ord             FlowLog's int32 cannot be widened to `number`: FlowLog
//                   has no numeric conversions, and a UDF taking its `ord`
//                   fails to compile (unused parentheses).
//   /, %            FlowLog's operators panic on a zero divisor.
// FlowLog's versions also work on int32, Souffle's `number` is int64.
//
// Partial functions. Souffle stops the program on `to_number` of a string
// without digits or out of range, and on `/` or `%` by zero or of INT64_MIN
// by -1 (SIGFPE). The ports return 0 there instead, because FlowLog calls
// functions on bindings Souffle never evaluates: before the rule's other
// conditions (flowlog-rs/flowlog B8) and on the rows a negation retracts
// (B9), and on clauses Souffle's optimiser deletes. Aborting in the call
// would stop runs Souffle completes. Each partial function has an `_error`
// companion, "" for arguments it is defined on and Souffle's complaint
// otherwise. The translator checks the calls Souffle surely evaluates with
// rules `FlowlogError(m) :- <body>, m = <f>_error(args), m != "".`, and
// `FlowlogAbort(n) :- FlowlogError(m), n = souffle_raise(m).` stops the run.
// ---------------------------------------------------------------------------

/// Stops the run with `msg`, as Souffle stops on an invalid call. Exits at
/// once rather than panic, which would leave the other workers waiting.
pub fn souffle_raise(msg: String) -> i64 {
    eprintln!("error: {msg}");
    std::process::exit(1)
}

pub fn souffle_strlen(s: String) -> i64 {
    s.len() as i64
}

/// `std::string::substr(i, n)` on the bytes: "" when `i` lies outside
/// 0..=len (Souffle also warns), the rest of the string when `n` is
/// negative. A cut inside a UTF-8 sequence, which Souffle emits as invalid
/// UTF-8, becomes U+FFFD.
pub fn souffle_substr(s: String, i: i64, n: i64) -> String {
    let b = s.as_bytes();
    let start = match usize::try_from(i) {
        Ok(start) if start <= b.len() => start,
        _ => return String::new(),
    };
    let end = usize::try_from(n).map_or(b.len(), |n| start.saturating_add(n).min(b.len()));
    if start == 0 && end == b.len() {
        return s;
    }
    String::from_utf8_lossy(&b[start..end]).into_owned()
}

/// `std::stoll(s, nullptr, base)`: skips leading white space, takes an
/// optional sign and (base 16) an optional 0x, then the longest run of
/// digits; `None` when there is none or the value overflows (stoll throws).
fn stoll(s: &str, base: u32) -> Option<i64> {
    let s = s.trim_start_matches([' ', '\t', '\n', '\x0b', '\x0c', '\r']);
    let (neg, s) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let s = match s.get(..2) {
        Some("0x" | "0X") if base == 16 && s[2..].starts_with(|c: char| c.is_ascii_hexdigit()) => &s[2..],
        _ => s,
    };
    let digits: Vec<u32> = s.chars().map_while(|c| c.to_digit(base)).collect();
    if digits.is_empty() {
        return None;
    }
    // saturate past i64's range, which try_from then rejects
    let mag = digits.iter().fold(0i128, |m, &d| (m * i128::from(base) + i128::from(d)).min(1 << 64));
    i64::try_from(if neg { -mag } else { mag }).ok()
}

/// RamSignedFromString(s, nullptr, 0): a `0b` or `0x` prefix, after an
/// optional `-`, selects base 2 or 16, anything else is base 10. `None`
/// where Souffle aborts.
fn to_number(s: &str) -> Option<i64> {
    if let Some(rest) = s.strip_prefix("0b") {
        stoll(rest, 2)
    } else if let Some(rest) = s.strip_prefix("-0b") {
        stoll(&format!("-{rest}"), 2)
    } else if s.starts_with("0x") || s.starts_with("-0x") {
        stoll(s, 16)
    } else {
        stoll(s, 10)
    }
}

/// Souffle's `to_number(s)`; 0 where Souffle aborts.
pub fn souffle_to_number(s: String) -> i64 {
    to_number(&s).unwrap_or(0)
}

pub fn souffle_to_number_error(s: String) -> String {
    match to_number(&s) {
        Some(_) => String::new(),
        None => format!("wrong string provided by `to_number({s:?})` functor."),
    }
}

/// Souffle's `a / b` on numbers (truncating); 0 where Souffle dies of
/// SIGFPE: `b = 0` and INT64_MIN / -1.
pub fn souffle_div(a: i64, b: i64) -> i64 {
    a.checked_div(b).unwrap_or(0)
}

/// Souffle's `a % b` on numbers (the sign of `a`); 0 where Souffle dies of
/// SIGFPE: `b = 0` and INT64_MIN % -1.
pub fn souffle_mod(a: i64, b: i64) -> i64 {
    a.checked_rem(b).unwrap_or(0)
}

fn signed_div_error(op: &str, a: i64, b: i64) -> String {
    if b == 0 {
        format!("{a} {op} 0: division by zero")
    } else if a == i64::MIN && b == -1 {
        format!("{a} {op} -1 overflows `number`")
    } else {
        String::new()
    }
}

pub fn souffle_div_error(a: i64, b: i64) -> String {
    signed_div_error("/", a, b)
}

pub fn souffle_mod_error(a: i64, b: i64) -> String {
    signed_div_error("%", a, b)
}

/// Souffle's `a / b` on `unsigned`; 0 for `b = 0`, where Souffle dies of
/// SIGFPE.
pub fn souffle_udiv(a: u64, b: u64) -> u64 {
    a.checked_div(b).unwrap_or(0)
}

pub fn souffle_umod(a: u64, b: u64) -> u64 {
    a.checked_rem(b).unwrap_or(0)
}

pub fn souffle_udiv_error(a: u64, b: u64) -> String {
    if b == 0 {
        format!("{a} / 0: division by zero")
    } else {
        String::new()
    }
}

pub fn souffle_umod_error(a: u64, b: u64) -> String {
    if b == 0 {
        format!("{a} % 0: division by zero")
    } else {
        String::new()
    }
}

/// Souffle's `a ^ b` on numbers: `static_cast<int64_t>(std::pow(a, b))`,
/// i.e. truncated. The cast is undefined in C++ outside int64 (and for an
/// infinite result such as 0 ^ -1); x86-64 gives INT64_MIN, as here.
pub fn souffle_pow(a: i64, b: i64) -> i64 {
    const LIMIT: f64 = 9_223_372_036_854_775_808.0; // 2^63
    let v = (a as f64).powf(b as f64).trunc();
    if v.is_finite() && (-LIMIT..LIMIT).contains(&v) {
        v as i64
    } else {
        i64::MIN
    }
}

/// Souffle's `ord(s)`, the symbol's id: FlowLog's `ord`, its interned key,
/// without the cast to int32.
pub fn souffle_ord(s: String) -> i64 {
    i64::from(::flowlog_runtime::intern::intern(&s).into_inner().get())
}

// ---------------------------------------------------------------------------
// Width anchors. FlowLog types an integer expression from its context and
// falls back to int32 where none pins it (a comparison of literals and
// count results, a `to_string` argument); `as(.., int64)` does not pin a
// literal (flowlog-rs/flowlog B11). The translator wraps such expressions in
// these identities, whose parameter types make FlowLog compute them on 64
// bits, as Souffle does.
// ---------------------------------------------------------------------------

pub fn flowlog_int64(x: i64) -> i64 {
    x
}

pub fn flowlog_uint64(x: u64) -> u64 {
    x
}

// ---------------------------------------------------------------------------
// min-choice helpers
//
// `R(keys.., deps..)` with `choice-domain (keys..)` is encoded as
// `R_choice(keys.., ck: min<int64>)` with `ck = choice_keyN(prio, deps..)`;
// `R` is decoded from the minimum key with `choice_field(ck, i)` (and
// `choice_number` for a field of type `number`).
//
// Key layout (always non-negative):
//   bits 54..62  prio  clause priority from the translator: 0..255 for the
//                      head's non-recursive clause versions, 256..511 for its
//                      recursive ones, each in the order Souffle evaluates
//                      them (source order permuted by its AST transformers,
//                      see souffle2flowlog.py, souffle_order_key). Souffle
//                      evaluates a stratum's non-recursive clauses first,
//                      then iterates the recursive ones, and keeps the first
//                      tuple derived for a key, so this mirrors its choice.
//   bits 43..53  len   total byte length of the dependent fields (saturating),
//                      a proxy for derivation depth (e.g. call strings).
//   bits  0..42  hash  FNV-1a of the fields, linear probing on collision.
// The key depends only on the candidate (barring 43-bit hash collisions), so
// the chosen tuple is identical across runs and worker counts. `len` and the
// hash read the fields joined by U+001F; the key's table stores them length
// prefixed, so fields containing U+001F still decode exactly.
//
// A choice relation read inside its own recursive stratum is read through
// `choice_claimN(inst, rel, ck, keys..)` instead: the first candidate key
// presented for (inst, rel, keys..) is kept and returned by every later call.
// FlowLog's static loops never retract a superseded lattice row, so the
// claim is what makes every in-loop read of a key see one witness, the
// first-round minimum in practice (see souffle2flowlog.py, loop_choice).
// ---------------------------------------------------------------------------

const FIELD_SEP: char = '\u{1f}';
const HASH_BITS: u32 = 43;
const HASH_MASK: u64 = (1 << HASH_BITS) - 1;
const LEN_BITS: u32 = 11;
const LEN_MAX: u64 = (1 << LEN_BITS) - 1;
const PRIO_BITS: u32 = 9;
const PRIO_MAX: i32 = (1 << PRIO_BITS) - 1;
const SHARDS: usize = 64;

type ChoiceShard = Mutex<HashMap<i64, Box<str>>>;

static CHOICE_MAP: LazyLock<Vec<ChoiceShard>> =
    LazyLock::new(|| (0..SHARDS).map(|_| Mutex::new(HashMap::new())).collect());

fn fnv1a(s: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in s.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h ^ (h >> HASH_BITS)
}

fn shard_of(key: i64) -> &'static ChoiceShard {
    &CHOICE_MAP[((key as u64) & HASH_MASK) as usize % SHARDS]
}

/// Each field as `<byte length>:<text>`.
fn encode_fields(fields: &[&str]) -> String {
    let mut out = String::new();
    for f in fields {
        out.push_str(&f.len().to_string());
        out.push(':');
        out.push_str(f);
    }
    out
}

/// Field `i` of an `encode_fields` text, if there are that many.
fn decode_field(enc: &str, i: usize) -> Option<&str> {
    let mut rest = enc;
    for k in 0.. {
        let colon = rest.find(':')?;
        let len: usize = rest[..colon].parse().ok()?;
        let field = rest.get(colon + 1..colon + 1 + len)?;
        if k == i {
            return Some(field);
        }
        rest = &rest[colon + 1 + len..];
    }
    None
}

fn choice_key(prio: i32, fields: &[&str]) -> i64 {
    let joined = fields.join(&FIELD_SEP.to_string());
    let enc = encode_fields(fields);
    let prio = prio.clamp(0, PRIO_MAX) as u64;
    let len = (joined.len() as u64).min(LEN_MAX);
    let hi = (prio << (HASH_BITS + LEN_BITS)) | (len << HASH_BITS);
    let mut h = fnv1a(&joined) & HASH_MASK;
    loop {
        let key = (hi | h) as i64;
        let mut shard = shard_of(key).lock().unwrap();
        match shard.get(&key) {
            Some(s) if **s == *enc => return key,
            Some(_) => h = (h + 1) & HASH_MASK,
            None => {
                shard.insert(key, enc.into_boxed_str());
                return key;
            }
        }
    }
}

pub fn choice_key1(prio: i32, a: String) -> i64 {
    choice_key(prio, &[&a])
}

pub fn choice_key2(prio: i32, a: String, b: String) -> i64 {
    choice_key(prio, &[&a, &b])
}

pub fn choice_key3(prio: i32, a: String, b: String, c: String) -> i64 {
    choice_key(prio, &[&a, &b, &c])
}

pub fn choice_key4(prio: i32, a: String, b: String, c: String, d: String) -> i64 {
    choice_key(prio, &[&a, &b, &c, &d])
}

pub fn choice_field(ck: i64, i: i32) -> String {
    let shard = shard_of(ck).lock().unwrap();
    let enc = shard.get(&ck).expect("unknown choice key");
    usize::try_from(i)
        .ok()
        .and_then(|i| decode_field(enc, i))
        .unwrap_or_default()
        .to_string()
}

/// Field `i` of a choice key, of type `number` (stored by `to_string`).
pub fn choice_number(ck: i64, i: i32) -> i64 {
    choice_field(ck, i).parse().expect("a choice key field of type number")
}

type ClaimShard = Mutex<HashMap<u128, i64>>;

static CLAIM_MAP: LazyLock<Vec<ClaimShard>> =
    LazyLock::new(|| (0..SHARDS).map(|_| Mutex::new(HashMap::new())).collect());

fn claim_hash(seed: u8, inst: i32, rel: i32, keys: &[&str]) -> u64 {
    let mut h = DefaultHasher::new();
    seed.hash(&mut h);
    inst.hash(&mut h);
    rel.hash(&mut h);
    for k in keys {
        k.hash(&mut h);
    }
    h.finish()
}

fn choice_claim(inst: i32, rel: i32, ck: i64, keys: &[&str]) -> i64 {
    let key = ((claim_hash(0, inst, rel, keys) as u128) << 64) | claim_hash(1, inst, rel, keys) as u128;
    let shard = &CLAIM_MAP[(key as usize) % SHARDS];
    *shard.lock().unwrap().entry(key).or_insert(ck)
}

pub fn choice_claim1(inst: i32, rel: i32, ck: i64, a: String) -> i64 {
    choice_claim(inst, rel, ck, &[&a])
}

pub fn choice_claim2(inst: i32, rel: i32, ck: i64, a: String, b: String) -> i64 {
    choice_claim(inst, rel, ck, &[&a, &b])
}

pub fn choice_claim3(inst: i32, rel: i32, ck: i64, a: String, b: String, c: String) -> i64 {
    choice_claim(inst, rel, ck, &[&a, &b, &c])
}

pub fn choice_claim4(inst: i32, rel: i32, ck: i64, a: String, b: String, c: String, d: String) -> i64 {
    choice_claim(inst, rel, ck, &[&a, &b, &c, &d])
}

pub fn choice_claim5(
    inst: i32,
    rel: i32,
    ck: i64,
    a: String,
    b: String,
    c: String,
    d: String,
    e: String,
) -> i64 {
    choice_claim(inst, rel, ck, &[&a, &b, &c, &d, &e])
}

pub fn choice_claim6(
    inst: i32,
    rel: i32,
    ck: i64,
    a: String,
    b: String,
    c: String,
    d: String,
    e: String,
    f: String,
) -> i64 {
    choice_claim(inst, rel, ck, &[&a, &b, &c, &d, &e, &f])
}
