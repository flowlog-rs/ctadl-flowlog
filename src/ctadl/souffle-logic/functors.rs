// Rust ports of CTADL's Souffle functors (functors.cpp), for the FlowLog
// engine (`ctadl index --engine flowlog`). The engine compiles them, after the
// translator's own UDFs (ctadl/flowlog/souffle2flowlog.rs), as the analysis'
// UDF file.
//
// FlowLog passes `string` parameters as owned `String`s and Souffle's `number`
// (FlowLog's `int64`) as `i64`. The functors taking `const char*` see a symbol
// only up to its first NUL; CTADL passes them call strings and access paths,
// built from Dex identifiers, which cannot contain U+0000, so the ports read
// whole strings.

const FRAME_SEP: char = '#';

fn push_frame_helper(function: &str, s: &str) -> String {
    let mut out = String::with_capacity(function.len() + 1 + s.len());
    out.push_str(function);
    out.push(FRAME_SEP);
    out.push_str(s);
    out
}

pub fn NewCallString() -> String {
    String::new()
}

pub fn CallStringLte(s1: String, s2: String) -> i64 {
    i64::from(s1.ends_with(s2.as_str()))
}

pub fn CallStringUnder(s1: String, s2: String) -> i64 {
    i64::from(s2.starts_with(s1.as_str()))
}

pub fn CallStringNonEmpty(s: String) -> i64 {
    i64::from(s.contains(FRAME_SEP))
}

pub fn CallStringSize(s: String) -> i64 {
    s.chars().filter(|&c| c == FRAME_SEP).count() as i64
}

pub fn TopFrame(stack: String) -> String {
    match stack.find(FRAME_SEP) {
        Some(i) => stack[..i].to_string(),
        None => String::new(),
    }
}

pub fn PushFrame(func: String, stack: String) -> String {
    push_frame_helper(&func, &stack)
}

/// Keeps the top `k - 1` frames of `stack` under `func`. CTADL builds its
/// functors with assertions on (no -DNDEBUG), so `k <= 0` aborts there; the
/// port treats it as `k = 1`, as an NDEBUG build does: an aborting UDF could
/// stop a run Souffle completes, since FlowLog may call it on a binding
/// Souffle never evaluates (see the builtins below).
pub fn PushFrameK(func: String, stack: String, k: i64) -> String {
    let bytes = stack.as_bytes();
    let mut i = 0usize;
    let mut count = 0i64;
    while i < bytes.len() && count + 1 < k {
        if bytes[i] == FRAME_SEP as u8 {
            count += 1;
        }
        i += 1;
    }
    if i < bytes.len() {
        push_frame_helper(&func, &stack[..i])
    } else {
        push_frame_helper(&func, &stack)
    }
}

pub fn PopFrame(dst: String) -> String {
    match dst.find(FRAME_SEP) {
        Some(i) => dst[i + 1..].to_string(),
        None => String::new(),
    }
}

pub fn AccessPathSize(ap: String) -> i64 {
    ap.bytes().filter(|&b| b == b'.').count() as i64
}

// Faithful port, including its substring-based notion of a repeated field.
pub fn AccessPathCycle(ap: String) -> i64 {
    let b = ap.as_bytes();
    for i in 0..b.len() {
        if b[i] != b'.' {
            continue;
        }
        let mut j = i + 1;
        while j < b.len() && b[j] != b'.' {
            j += 1;
        }
        let field = &ap[i..j];
        if let Some(pos) = ap.find(field) {
            if pos != i {
                return 1;
            }
            if let Some(off) = ap[pos + 1..].find(field) {
                if pos + 1 + off != i {
                    return 1;
                }
            }
        }
    }
    0
}

pub fn AndroidManifestClassId(manifest_name: String) -> String {
    format!("L{manifest_name};").replace('.', "/")
}

pub fn CheckSubstring(s1: String, s2: String) -> i64 {
    if s1.contains(s2.as_str()) {
        1
    } else if s2.contains(s1.as_str()) {
        2
    } else {
        0
    }
}
