"""C++17 / C++20 execution harness.

The snippet under test (a free function, a namespaced function, or a
LeetCode-style ``class Solution`` method) is compiled together with a
header-only harness that speaks the same JSON-lines protocol as the Python
worker.  Argument and return types are taken from the function pointer at
compile time (no textual type parsing at runtime), so any combination of
integers, floating point, ``bool``, ``char``, ``std::string``, ``vector``,
``deque``, ``list``, ``array``, ``set``, ``unordered_set``, ``map``,
``unordered_map``, ``pair``, ``tuple`` and ``optional`` works.  ``void``
functions report their (possibly mutated) arguments instead of a return value.

Binaries are cached by content hash.  The compiler is ``$CXX`` (default
``g++``), falling back to ``clang++``, ``c++`` and finally ``python -m ziglang
c++`` when the ``ziglang`` wheel is installed.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from counterexample.adapters.base import ProcessSide, WorkerError, sandbox_env
from counterexample.adapters.cpp_outline import CppDef, outline
from counterexample.config import get_settings
from counterexample.generators import SpecError, spec_from_cpp_type

HARNESS_VERSION = "1"

PRELUDE = r"""
// ===== counterexample harness prelude (do not edit) =====
#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <exception>
#include <functional>
#include <iostream>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <numeric>
#include <optional>
#include <queue>
#include <set>
#include <sstream>
#include <stack>
#include <stdexcept>
#include <string>
#include <tuple>
#include <type_traits>
#include <typeinfo>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
#if defined(__has_include)
#  if __has_include(<cxxabi.h>)
#    include <cxxabi.h>
#    define CX_HAS_CXXABI 1
#  endif
#  if __has_include(<sys/resource.h>)
#    include <sys/resource.h>
#    define CX_HAS_RLIMIT 1
#  endif
#endif

namespace cx {

struct J {
  enum Kind { NUL, BOOL, NUM, STR, ARR, OBJ } k = NUL;
  bool b = false;
  bool is_int = false;
  long long i = 0;
  double d = 0;
  std::string s;
  std::vector<J> a;
  std::vector<std::pair<std::string, J>> o;
  const J* get(const std::string& key) const {
    for (auto& kv : o) if (kv.first == key) return &kv.second;
    return nullptr;
  }
};

struct Parser {
  const std::string& t;
  size_t p = 0;
  explicit Parser(const std::string& text) : t(text) {}
  [[noreturn]] void fail(const char* m) { throw std::runtime_error(std::string("bad json: ") + m); }
  void ws() { while (p < t.size() && (t[p] == ' ' || t[p] == '\n' || t[p] == '\r' || t[p] == '\t')) ++p; }
  char peek() { if (p >= t.size()) fail("eof"); return t[p]; }
  unsigned hex4() {
    unsigned v = 0;
    for (int k = 0; k < 4; ++k) {
      char c = peek(); ++p; v <<= 4;
      if (c >= '0' && c <= '9') v |= unsigned(c - '0');
      else if (c >= 'a' && c <= 'f') v |= unsigned(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= unsigned(c - 'A' + 10);
      else fail("hex");
    }
    return v;
  }
  static void utf8(std::string& out, unsigned cp) {
    if (cp < 0x80) out += char(cp);
    else if (cp < 0x800) { out += char(0xC0 | (cp >> 6)); out += char(0x80 | (cp & 0x3F)); }
    else if (cp < 0x10000) { out += char(0xE0 | (cp >> 12)); out += char(0x80 | ((cp >> 6) & 0x3F)); out += char(0x80 | (cp & 0x3F)); }
    else { out += char(0xF0 | (cp >> 18)); out += char(0x80 | ((cp >> 12) & 0x3F)); out += char(0x80 | ((cp >> 6) & 0x3F)); out += char(0x80 | (cp & 0x3F)); }
  }
  std::string str() {
    if (peek() != '"') fail("string");
    ++p;
    std::string out;
    while (true) {
      char c = peek(); ++p;
      if (c == '"') break;
      if (c != '\\') { out += c; continue; }
      char e = peek(); ++p;
      switch (e) {
        case 'n': out += '\n'; break;
        case 't': out += '\t'; break;
        case 'r': out += '\r'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case '/': out += '/'; break;
        case '\\': out += '\\'; break;
        case '"': out += '"'; break;
        case 'u': {
          unsigned cp = hex4();
          if (cp >= 0xD800 && cp <= 0xDBFF && p + 1 < t.size() && t[p] == '\\' && t[p + 1] == 'u') {
            p += 2;
            unsigned lo = hex4();
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          }
          utf8(out, cp);
          break;
        }
        default: fail("escape");
      }
    }
    return out;
  }
  J parse() {
    ws();
    char c = peek();
    J v;
    if (c == '{') {
      ++p; v.k = J::OBJ; ws();
      if (peek() == '}') { ++p; return v; }
      while (true) {
        ws(); std::string key = str(); ws();
        if (peek() != ':') fail("colon");
        ++p;
        J val = parse();
        v.o.emplace_back(std::move(key), std::move(val));
        ws();
        if (peek() == ',') { ++p; continue; }
        if (peek() == '}') { ++p; break; }
        fail("object");
      }
      return v;
    }
    if (c == '[') {
      ++p; v.k = J::ARR; ws();
      if (peek() == ']') { ++p; return v; }
      while (true) {
        v.a.push_back(parse()); ws();
        if (peek() == ',') { ++p; continue; }
        if (peek() == ']') { ++p; break; }
        fail("array");
      }
      return v;
    }
    if (c == '"') { v.k = J::STR; v.s = str(); return v; }
    if (t.compare(p, 4, "true") == 0) { p += 4; v.k = J::BOOL; v.b = true; return v; }
    if (t.compare(p, 5, "false") == 0) { p += 5; v.k = J::BOOL; return v; }
    if (t.compare(p, 4, "null") == 0) { p += 4; return v; }
    size_t st = p;
    bool isint = true;
    if (t[p] == '-' || t[p] == '+') ++p;
    while (p < t.size() && (std::isdigit((unsigned char)t[p]) || t[p] == '.' || t[p] == 'e' || t[p] == 'E' || t[p] == '-' || t[p] == '+')) {
      if (t[p] == '.' || t[p] == 'e' || t[p] == 'E') isint = false;
      ++p;
    }
    if (p == st) fail("value");
    v.k = J::NUM;
    v.s = t.substr(st, p - st);
    v.d = std::strtod(v.s.c_str(), nullptr);
    v.is_int = isint;
    if (isint) { errno = 0; v.i = std::strtoll(v.s.c_str(), nullptr, 10); }
    return v;
  }
};

inline const J& untag(const J& v) {
  if (v.k == J::OBJ && !v.o.empty()) {
    const std::string& tag = v.o[0].first;
    if (tag == "$tuple" || tag == "$set" || tag == "$frozenset" || tag == "$dict") return v.o[0].second;
  }
  return v;
}

inline double num_of(const J& v) {
  if (v.k == J::NUM) return v.d;
  if (v.k == J::BOOL) return v.b ? 1.0 : 0.0;
  if (v.k == J::STR) return std::strtod(v.s.c_str(), nullptr);
  if (v.k == J::OBJ && !v.o.empty() && v.o[0].first == "$float") {
    const std::string& s = v.o[0].second.s;
    if (s == "nan") return std::numeric_limits<double>::quiet_NaN();
    if (s == "inf") return std::numeric_limits<double>::infinity();
    if (s == "-inf") return -std::numeric_limits<double>::infinity();
  }
  throw std::runtime_error("expected a number");
}

inline void esc(std::string& o, const std::string& s) {
  o += '"';
  for (unsigned char c : s) {
    switch (c) {
      case '"': o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\n': o += "\\n"; break;
      case '\r': o += "\\r"; break;
      case '\t': o += "\\t"; break;
      default:
        if (c < 0x20) { char buf[8]; std::snprintf(buf, sizeof buf, "\\u%04x", c); o += buf; }
        else o += char(c);
    }
  }
  o += '"';
}

template <class T, class Enable = void> struct Conv {
  static_assert(sizeof(T) == 0, "counterexample: unsupported parameter/return type for the C++ harness");
};

template <class T> struct Conv<T, std::enable_if_t<std::is_same_v<T, bool>>> {
  static T from(const J& v) { return v.k == J::BOOL ? v.b : num_of(v) != 0; }
  static void to(std::string& o, const T& x) { o += x ? "true" : "false"; }
};

template <class T> struct Conv<T, std::enable_if_t<std::is_same_v<T, char>>> {
  static T from(const J& v) {
    if (v.k == J::STR) return v.s.empty() ? '\0' : v.s[0];
    return char(num_of(v));
  }
  static void to(std::string& o, const T& x) { esc(o, std::string(1, x)); }
};

template <class T> struct Conv<T, std::enable_if_t<std::is_integral_v<T> && !std::is_same_v<T, bool> && !std::is_same_v<T, char>>> {
  static T from(const J& v) {
    if (v.k == J::NUM && v.is_int) {
      if constexpr (std::is_unsigned_v<T>) return T(std::strtoull(v.s.c_str(), nullptr, 10));
      else return T(v.i);
    }
    if (v.k == J::BOOL) return T(v.b);
    if (v.k == J::STR) {
      if constexpr (std::is_unsigned_v<T>) return T(std::strtoull(v.s.c_str(), nullptr, 10));
      else return T(std::strtoll(v.s.c_str(), nullptr, 10));
    }
    return T(num_of(v));
  }
  static void to(std::string& o, const T& x) {
    if constexpr (std::is_unsigned_v<T>) o += std::to_string((unsigned long long)x);
    else o += std::to_string((long long)x);
  }
};

template <class T> struct Conv<T, std::enable_if_t<std::is_floating_point_v<T>>> {
  static T from(const J& v) { return T(num_of(v)); }
  static void to(std::string& o, const T& x) {
    double d = double(x);
    if (std::isnan(d)) { o += "{\"$float\":\"nan\"}"; return; }
    if (std::isinf(d)) { o += d > 0 ? "{\"$float\":\"inf\"}" : "{\"$float\":\"-inf\"}"; return; }
    char buf[40];
    std::snprintf(buf, sizeof buf, "%.17g", d);
    o += buf;
    if (!std::strpbrk(buf, ".eE")) o += ".0";
  }
};

template <> struct Conv<std::string, void> {
  static std::string from(const J& v) {
    if (v.k == J::STR) return v.s;
    if (v.k == J::NUM) return v.s;
    throw std::runtime_error("expected a string");
  }
  static void to(std::string& o, const std::string& x) { esc(o, x); }
};

template <class Seq> void seq_to(std::string& o, const Seq& xs) {
  o += '[';
  bool first = true;
  for (const auto& x : xs) {
    if (!first) o += ',';
    first = false;
    Conv<std::decay_t<decltype(x)>>::to(o, x);
  }
  o += ']';
}

#define CX_SEQ(TPL)                                                               \
  template <class T, class A> struct Conv<TPL<T, A>, void> {                      \
    static TPL<T, A> from(const J& v) {                                           \
      const J& u = untag(v);                                                      \
      if (u.k != J::ARR) throw std::runtime_error("expected an array");           \
      TPL<T, A> out;                                                              \
      for (const auto& e : u.a) out.push_back(Conv<T>::from(e));                  \
      return out;                                                                 \
    }                                                                             \
    static void to(std::string& o, const TPL<T, A>& xs) { seq_to(o, xs); }        \
  };
CX_SEQ(std::vector)
CX_SEQ(std::deque)
CX_SEQ(std::list)

template <class A> struct Conv<std::vector<bool, A>, void> {
  static std::vector<bool, A> from(const J& v) {
    const J& u = untag(v);
    std::vector<bool, A> out;
    for (const auto& e : u.a) out.push_back(Conv<bool>::from(e));
    return out;
  }
  static void to(std::string& o, const std::vector<bool, A>& xs) {
    o += '[';
    for (size_t i = 0; i < xs.size(); ++i) { if (i) o += ','; o += xs[i] ? "true" : "false"; }
    o += ']';
  }
};

template <class T, size_t N> struct Conv<std::array<T, N>, void> {
  static std::array<T, N> from(const J& v) {
    const J& u = untag(v);
    std::array<T, N> out{};
    for (size_t i = 0; i < N && i < u.a.size(); ++i) out[i] = Conv<T>::from(u.a[i]);
    return out;
  }
  static void to(std::string& o, const std::array<T, N>& xs) { seq_to(o, xs); }
};

#define CX_SET(TPL, TAGGED)                                                       \
  template <class T, class... R> struct Conv<TPL<T, R...>, void> {                \
    static TPL<T, R...> from(const J& v) {                                        \
      const J& u = untag(v);                                                      \
      TPL<T, R...> out;                                                           \
      for (const auto& e : u.a) out.insert(Conv<T>::from(e));                     \
      return out;                                                                 \
    }                                                                             \
    static void to(std::string& o, const TPL<T, R...>& xs) {                      \
      if (TAGGED) o += "{\"$set\":";                                              \
      seq_to(o, xs);                                                              \
      if (TAGGED) o += '}';                                                       \
    }                                                                             \
  };
CX_SET(std::set, true)
CX_SET(std::unordered_set, true)
CX_SET(std::multiset, false)

#define CX_MAP(TPL)                                                               \
  template <class K, class V, class... R> struct Conv<TPL<K, V, R...>, void> {    \
    static TPL<K, V, R...> from(const J& v) {                                     \
      TPL<K, V, R...> out;                                                        \
      if (v.k == J::OBJ && !v.o.empty() && v.o[0].first == "$dict") {            \
        for (const auto& kv : v.o[0].second.a)                                    \
          out.emplace(Conv<K>::from(kv.a.at(0)), Conv<V>::from(kv.a.at(1)));      \
      } else if (v.k == J::OBJ) {                                                 \
        for (const auto& kv : v.o) {                                              \
          J key; key.k = J::STR; key.s = kv.first;                                \
          out.emplace(Conv<K>::from(key), Conv<V>::from(kv.second));              \
        }                                                                         \
      } else throw std::runtime_error("expected an object");                      \
      return out;                                                                 \
    }                                                                             \
    static void to(std::string& o, const TPL<K, V, R...>& m) {                    \
      o += "{\"$dict\":[";                                                        \
      bool first = true;                                                          \
      for (const auto& kv : m) {                                                  \
        if (!first) o += ',';                                                     \
        first = false;                                                            \
        o += '[';                                                                 \
        Conv<K>::to(o, kv.first);                                                 \
        o += ',';                                                                 \
        Conv<V>::to(o, kv.second);                                                \
        o += ']';                                                                 \
      }                                                                           \
      o += "]}";                                                                  \
    }                                                                             \
  };
CX_MAP(std::map)
CX_MAP(std::unordered_map)

template <class A, class B> struct Conv<std::pair<A, B>, void> {
  static std::pair<A, B> from(const J& v) {
    const J& u = untag(v);
    if (u.k != J::ARR || u.a.size() != 2) throw std::runtime_error("expected a pair");
    return {Conv<A>::from(u.a[0]), Conv<B>::from(u.a[1])};
  }
  static void to(std::string& o, const std::pair<A, B>& x) {
    o += "{\"$tuple\":[";
    Conv<A>::to(o, x.first);
    o += ',';
    Conv<B>::to(o, x.second);
    o += "]}";
  }
};

template <class... T> struct Conv<std::tuple<T...>, void> {
  template <size_t... I> static std::tuple<T...> build(const J& u, std::index_sequence<I...>) {
    return std::tuple<T...>{Conv<T>::from(u.a.at(I))...};
  }
  static std::tuple<T...> from(const J& v) {
    const J& u = untag(v);
    if (u.k != J::ARR || u.a.size() != sizeof...(T)) throw std::runtime_error("expected a tuple");
    return build(u, std::index_sequence_for<T...>{});
  }
  static void to(std::string& o, const std::tuple<T...>& x) {
    o += "{\"$tuple\":[";
    bool first = true;
    std::apply([&](const auto&... e) { ((o += (first ? "" : ","), first = false, Conv<std::decay_t<decltype(e)>>::to(o, e)), ...); }, x);
    o += "]}";
  }
};

template <class T> struct Conv<std::optional<T>, void> {
  static std::optional<T> from(const J& v) {
    if (v.k == J::NUL) return std::nullopt;
    return Conv<T>::from(v);
  }
  static void to(std::string& o, const std::optional<T>& x) {
    if (!x) o += "null";
    else Conv<T>::to(o, *x);
  }
};

template <class F> struct Traits;
template <class R, class... A> struct Traits<R (*)(A...)> { using Ret = R; using Args = std::tuple<A...>; using Cls = void; };
template <class R, class... A> struct Traits<R (*)(A...) noexcept> : Traits<R (*)(A...)> {};
template <class R, class C, class... A> struct Traits<R (C::*)(A...)> { using Ret = R; using Args = std::tuple<A...>; using Cls = C; };
template <class R, class C, class... A> struct Traits<R (C::*)(A...) const> : Traits<R (C::*)(A...)> {};
template <class R, class C, class... A> struct Traits<R (C::*)(A...) noexcept> : Traits<R (C::*)(A...)> {};
template <class R, class C, class... A> struct Traits<R (C::*)(A...) const noexcept> : Traits<R (C::*)(A...)> {};

inline std::string demangle(const char* name) {
#ifdef CX_HAS_CXXABI
  int status = 0;
  char* out = abi::__cxa_demangle(name, nullptr, nullptr, &status);
  if (status == 0 && out) { std::string s(out); std::free(out); return s; }
#endif
  return name;
}

template <class F> struct Entry {
  using Tr = Traits<F>;
  using Args = typename Tr::Args;
  using Cls = typename Tr::Cls;
  using R = typename Tr::Ret;
  static constexpr size_t N = std::tuple_size_v<Args>;
  template <size_t I> using Arg = std::tuple_element_t<I, Args>;
  template <size_t I> using Val = std::decay_t<Arg<I>>;

  template <size_t... I> static auto parse(const J& args, std::index_sequence<I...>) {
    const J& u = untag(args);
    if (u.k != J::ARR || u.a.size() != N)
      throw std::invalid_argument("expected " + std::to_string(N) + " argument(s), got " + std::to_string(u.a.size()));
    return std::tuple<Val<I>...>{Conv<Val<I>>::from(u.a[I])...};
  }

  template <class Tup, size_t... I> static decltype(auto) invoke(F f, Tup& vals, std::index_sequence<I...>) {
    if constexpr (std::is_void_v<Cls>) {
      return f(static_cast<Arg<I>>(std::get<I>(vals))...);
    } else {
      Cls obj{};
      return (obj.*f)(static_cast<Arg<I>>(std::get<I>(vals))...);
    }
  }

  template <class Tup, size_t... I> static void args_to(std::string& o, const Tup& vals, std::index_sequence<I...>) {
    o += '[';
    bool first = true;
    ((o += (first ? "" : ","), first = false, Conv<Val<I>>::to(o, std::get<I>(vals))), ...);
    o += ']';
  }

  static std::string call(F f, const J& args, long long& ns, bool observe) {
    using Idx = std::make_index_sequence<N>;
    auto vals = parse(args, Idx{});
    std::string out = ",\"out\":";
    auto t0 = std::chrono::steady_clock::now();
    if constexpr (std::is_void_v<R>) {
      invoke(f, vals, Idx{});
      ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count();
      out += "null";
      observe = true;
    } else {
      auto&& r = invoke(f, vals, Idx{});
      ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count();
      Conv<std::decay_t<R>>::to(out, r);
    }
    if (observe) {
      out += ",\"args_after\":";
      args_to(out, vals, Idx{});
    }
    return out;
  }

  static std::string profile(F f, const J& args, int repeat, long long min_ns, long long budget_ns) {
    using Idx = std::make_index_sequence<N>;
    auto base = parse(args, Idx{});
    std::vector<double> samples;
    auto start = std::chrono::steady_clock::now();
    auto elapsed = [&] { return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count(); };
    for (int r = 0; r < repeat; ++r) {
      long long total = 0;
      long long loops = 0;
      while (true) {
        auto vals = base;
        auto t0 = std::chrono::steady_clock::now();
        if constexpr (std::is_void_v<R>) invoke(f, vals, Idx{});
        else { auto&& res = invoke(f, vals, Idx{}); (void)res; }
        total += std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count();
        ++loops;
        if (total >= min_ns || loops >= 1000 || elapsed() > budget_ns) break;
      }
      samples.push_back(double(total) / double(loops));
      if (elapsed() > budget_ns) break;
    }
    std::sort(samples.begin(), samples.end());
    char buf[96];
    std::snprintf(buf, sizeof buf, ",\"ns_min\":%.1f,\"ns_median\":%.1f,\"samples\":%d,\"peak_bytes\":null", samples.front(), samples[samples.size() / 2], int(samples.size()));
    return buf;
  }
};

inline void reply(const std::string& body) {
  std::fwrite(body.data(), 1, body.size(), stdout);
  std::fputc('\n', stdout);
  std::fflush(stdout);
}

template <class F> int serve(F f, const char* entry_name) {
#ifdef CX_HAS_RLIMIT
  if (const char* mb = std::getenv("CX_MEMORY_MB")) {
    long long lim = std::atoll(mb) * 1024LL * 1024LL;
    if (lim > 0) { struct rlimit rl; rl.rlim_cur = rl.rlim_max = (rlim_t)lim; setrlimit(RLIMIT_AS, &rl); }
  }
#endif
  std::ios::sync_with_stdio(false);
  {
    std::string hello = "{\"ok\":true,\"entry\":";
    esc(hello, entry_name);
    hello += "}";
    reply(hello);
  }
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    J msg;
    std::string id = "null";
    try {
      msg = Parser(line).parse();
      if (const J* idv = msg.get("id")) if (idv->k == J::NUM) id = idv->s;
    } catch (const std::exception& e) {
      std::string o = "{\"id\":null,\"ok\":false,\"exc\":\"ProtocolError\",\"msg\":";
      esc(o, e.what());
      reply(o + "}");
      continue;
    }
    const J* op = msg.get("op");
    std::string opname = op ? op->s : "";
    if (opname == "exit") break;
    static const J empty_args = [] { J j; j.k = J::ARR; return j; }();
    const J* args = msg.get("args");
    if (!args) args = &empty_args;
    long long ns = 0;
    auto t0 = std::chrono::steady_clock::now();
    try {
      std::string body;
      if (opname == "call") {
        const J* obs = msg.get("observe_args");
        body = Entry<F>::call(f, *args, ns, obs && obs->k == J::BOOL && obs->b);
      } else if (opname == "profile") {
        const J* rep = msg.get("repeat");
        const J* mn = msg.get("min_time_ns");
        const J* bud = msg.get("budget_ns");
        body = Entry<F>::profile(f, *args, rep ? int(rep->d) : 5, mn ? (long long)mn->d : 2000000LL, bud ? (long long)bud->d : 5000000000LL);
      } else {
        throw std::invalid_argument("unknown op " + opname);
      }
      reply("{\"id\":" + id + ",\"ok\":true,\"ns\":" + std::to_string(ns) + body + "}");
    } catch (const std::exception& e) {
      if (!ns) ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count();
      std::string o = "{\"id\":" + id + ",\"ok\":false,\"ns\":" + std::to_string(ns) + ",\"exc\":";
      esc(o, demangle(typeid(e).name()));
      o += ",\"msg\":";
      esc(o, demangle(typeid(e).name()) + ": " + e.what());
      reply(o + "}");
    } catch (...) {
      reply("{\"id\":" + id + ",\"ok\":false,\"ns\":" + std::to_string(ns) + ",\"exc\":\"unknown\",\"msg\":\"non-std exception thrown\"}");
    }
  }
  return 0;
}

}  // namespace cx
// ===== end of prelude =====
"""

MAIN_TEMPLATE = r"""
#undef main
#undef int
int main() { return cx::serve(&{entry_expr}, "{entry_name}"); }
"""

STDCXX_SHIM = """// Portable stand-in for GCC's <bits/stdc++.h> (used with clang/zig/libc++).
#include <algorithm>
#include <array>
#include <bitset>
#include <cassert>
#include <cctype>
#include <climits>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <numeric>
#include <optional>
#include <queue>
#include <random>
#include <set>
#include <sstream>
#include <stack>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
"""

ROLE_PREFERENCES = {
    "oracle": ["oracle", "brute_force", "bruteforce", "reference", "naive", "solve", "solution"],
    "candidate": ["candidate", "optimized", "fast", "solve", "solution"],
}

_compiler_lock = threading.Lock()
_compiler_cache: list[list[str]] = []


def cache_dir() -> Path:
    path = Path(os.environ.get("COUNTEREXAMPLE_CACHE", Path(tempfile.gettempdir()) / "counterexample-cache")) / "cpp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_compiler() -> list[str]:
    """Return the compiler command as argv, or raise WorkerError."""
    with _compiler_lock:
        if _compiler_cache:
            return _compiler_cache[0]
        configured = os.environ.get("CXX")
        candidates: list[list[str]] = []
        if configured:
            candidates.append(shlex.split(configured, posix=os.name != "nt"))
        candidates += [["g++"], ["clang++"], ["c++"]]
        for argv in candidates:
            if shutil.which(argv[0]):
                _compiler_cache.append(argv)
                return argv
        try:
            import ziglang  # noqa: F401

            argv = [sys.executable, "-m", "ziglang", "c++"]
            _compiler_cache.append(argv)
            return argv
        except ImportError:
            pass
    raise WorkerError(
        "no C++ compiler found: install g++ or clang++ (or `pip install ziglang`), or set $CXX",
        {"phase": "compile"},
    )


def _resolve_entry(defs: list[CppDef], role: str, explicit: str | None) -> CppDef:
    callables = [d for d in defs if d.kind in ("function", "method") and d.name != "main" and not d.name.startswith("~")]
    if explicit:
        want = explicit.replace(".", "::")
        for d in callables:
            if d.qualified == want or d.name == want or (d.cls and f"{d.cls}::{d.name}" == want):
                return d
        raise WorkerError(f"entrypoint {explicit!r} not found in {role} code; found: {', '.join(d.qualified for d in callables) or 'nothing'}")
    free = [d for d in callables if d.kind == "function"]
    for name in ROLE_PREFERENCES.get(role, []):
        for d in free:
            if d.name == name:
                return d
    if len(free) == 1:
        return free[0]
    sol = [d for d in callables if d.cls == "Solution" and d.access == "public" and d.name != "Solution"]
    if sol:
        return sol[0]
    if free:
        return free[-1]
    methods = [d for d in callables if d.access == "public" and d.name != d.cls]
    if methods:
        return methods[0]
    raise WorkerError(f"no function found in {role} code; pass an explicit entrypoint")


class CppImplementation:
    language = "cpp"

    def __init__(self, code: str, *, role: str, entrypoint: str | None = None, std: str | None = None):
        self.code = code
        self.role = role
        self.settings = get_settings()
        self.std = (std or self.settings.cxx_std or "c++17").lower()
        if not self.std.startswith(("c++", "gnu++")):
            self.std = "c++" + self.std
        self.defn = _resolve_entry(outline(code), role, entrypoint)
        self.entrypoint = self.defn.qualified
        self.exe: Path | None = None
        self.compile_output = ""

    @property
    def signature(self) -> dict[str, Any]:
        return {"name": self.defn.qualified, "return": self.defn.ret, "params": self.defn.params, "class": self.defn.cls}

    def infer_spec(self) -> dict[str, Any]:
        try:
            return {"args": [spec_from_cpp_type(t) for t in self.defn.params]}
        except SpecError:
            raise

    def source(self) -> str:
        entry_expr = self.defn.qualified
        main = MAIN_TEMPLATE.replace("{entry_expr}", entry_expr).replace("{entry_name}", self.defn.qualified)
        return f"{PRELUDE}\n#define main cx_user_main\n#line 1 \"{self.role}.cpp\"\n{self.code}\n{main}"

    def prepare(self) -> None:
        compiler = find_compiler()
        extra = shlex.split(os.environ.get("CXX_FLAGS", ""), posix=os.name != "nt")
        flags = [f"-std={self.std}", "-O2"] + extra
        src = self.source()
        key = hashlib.sha256("\0".join([HARNESS_VERSION, " ".join(compiler), " ".join(flags), src]).encode()).hexdigest()[:24]
        root = cache_dir()
        exe = root / (f"{self.role}-{key}" + (".exe" if os.name == "nt" else ""))
        if exe.exists():
            self.exe = exe
            return
        shim = root / "shim" / "bits"
        shim.mkdir(parents=True, exist_ok=True)
        shim_file = shim / "stdc++.h"
        if not shim_file.exists():
            shim_file.write_text(STDCXX_SHIM, encoding="utf-8")
        src_path = root / f"{self.role}-{key}.cpp"
        src_path.write_text(src, encoding="utf-8")
        cmd = compiler + flags + ["-idirafter", str(root / "shim"), "-o", str(exe), str(src_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240, cwd=str(root))
        except subprocess.TimeoutExpired as exc:
            raise WorkerError(f"{self.role} compilation timed out", {"phase": "compile"}) from exc
        except OSError as exc:
            raise WorkerError(f"could not run the C++ compiler {compiler[0]!r}: {exc}", {"phase": "compile"}) from exc
        self.compile_output = (proc.stderr or "") + (proc.stdout or "")
        if proc.returncode != 0 or not exe.exists():
            raise WorkerError(
                f"{self.role} failed to compile",
                {"phase": "compile", "compiler": " ".join(compiler), "compiler_output": _trim_compiler_output(self.compile_output, self.role)},
            )
        self.exe = exe

    def spawn(self, timeout_s: float) -> ProcessSide:
        if self.exe is None:
            self.prepare()
        assert self.exe is not None
        env = sandbox_env({"CX_MEMORY_MB": str(self.settings.sandbox_memory_mb)} if "sanitize" not in os.environ.get("CXX_FLAGS", "") else None)
        return ProcessSide([str(self.exe)], label=self.role, setup=None, timeout_s=timeout_s, env=env)

    def describe(self) -> dict[str, Any]:
        return {"language": self.language, "entrypoint": self.entrypoint, "std": self.std, "signature": self.signature}

    @property
    def supports_trace(self) -> bool:
        return False


def _trim_compiler_output(text: str, role: str, limit: int = 6000) -> str:
    """Keep the diagnostics that point into the user's code first."""
    lines = text.splitlines()
    mine = [ln for ln in lines if f"{role}.cpp" in ln]
    rest = [ln for ln in lines if f"{role}.cpp" not in ln]
    out = "\n".join(mine[:80] + (["..."] if mine else []) + rest[:80])
    out = re.sub(r"[A-Za-z]:\\[^\s:]*counterexample-cache[^\s:]*", "<harness>", out)
    return out[:limit]
