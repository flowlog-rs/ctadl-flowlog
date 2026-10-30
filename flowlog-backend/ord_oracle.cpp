// Soufflé functor for ord_oracle.sh: oord(x) is x's `ord` in another engine's
// numbering, read from the TSV file $OORD_MAP (symbol, number). A symbol the
// file lacks gets a number past all of the file's, the same on every call.
// CTADL builds Soufflé with 64-bit numbers.
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <string>
#include <unordered_map>

namespace {
struct Table {
  std::unordered_map<std::string, std::int64_t> known, extra;
  std::int64_t next = 0;
  std::mutex mu;
  Table() {
    const char *path = std::getenv("OORD_MAP");
    std::ifstream in(path ? path : "");
    if (!in) {
      std::fprintf(stderr, "oord: cannot read $OORD_MAP\n");
      std::abort();
    }
    std::string line;
    while (std::getline(in, line)) {
      auto tab = line.rfind('\t');
      std::int64_t v = std::stoll(line.substr(tab + 1));
      known.emplace(line.substr(0, tab), v);
      next = std::max(next, v + 1);
    }
    std::fprintf(stderr, "oord: %zu ords from %s\n", known.size(), path);
  }
  ~Table() {
    std::fprintf(stderr, "oord: %zu symbols numbered past them\n", extra.size());
  }
};
Table &table() {
  static Table t;
  return t;
}
} // namespace

extern "C" std::int64_t oord(const char *x) {
  Table &t = table();
  auto it = t.known.find(x);
  if (it != t.known.end())
    return it->second;
  std::lock_guard<std::mutex> lock(t.mu);
  auto [jt, fresh] = t.extra.try_emplace(x, t.next);
  if (fresh)
    ++t.next;
  return jt->second;
}
