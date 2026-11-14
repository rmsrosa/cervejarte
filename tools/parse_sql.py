"""Parse a phpMyAdmin/mysqldump file into JSON rows per table (no DB needed)."""
import json, re, sys

WANT = {"wp_posts", "wp_comments", "wp_terms", "wp_term_taxonomy",
        "wp_term_relationships", "wp_postmeta", "wp_options", "wp_users"}
# Columns never exported (privacy).
DROP = {"wp_comments": {"comment_author_email", "comment_author_IP", "comment_agent"},
        "wp_users": {"user_email", "user_pass", "user_activation_key", "user_login", "user_url"}}
ESC = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "b": "\b"}


def parse_values(s, i):
    """Parse '(..),(..);' starting at s[i]; yield tuples."""
    n = len(s)
    while i < n:
        while s[i] in " \n\r\t,":
            i += 1
        if s[i] == ";":
            return i + 1
        assert s[i] == "(", s[i:i + 50]
        i += 1
        row = []
        while True:
            while s[i] in " \n\r\t":
                i += 1
            c = s[i]
            if c == "'":
                i += 1
                buf = []
                while True:
                    j = s.find("'", i)
                    k = s.find("\\", i, j)
                    if k != -1:
                        buf.append(s[i:k]); e = s[k + 1]
                        buf.append(ESC.get(e, e)); i = k + 2
                        continue
                    if s[j + 1:j + 2] == "'":
                        buf.append(s[i:j] + "'"); i = j + 2
                        continue
                    buf.append(s[i:j]); i = j + 1
                    break
                row.append("".join(buf))
            else:
                m = re.compile(r"[^,)]+").match(s, i)
                tok = m.group(0).strip(); i = m.end()
                row.append(None if tok == "NULL" else tok)
            while s[i] in " \n\r\t":
                i += 1
            if s[i] == ",":
                i += 1
                continue
            assert s[i] == ")"
            i += 1
            break
        yield_rows.append(row)
    return i


yield_rows = []


def main(sql_path, out_path):
    s = open(sql_path, encoding="utf-8", errors="replace").read()
    out = {}
    ins = re.compile(r"INSERT INTO `(\w+)` \(([^)]*)\) VALUES\s*")
    pos = 0
    while True:
        m = ins.search(s, pos)
        if not m:
            break
        table = m.group(1)
        cols = [c.strip(" `") for c in m.group(2).split(",")]
        yield_rows.clear()
        pos = parse_values(s, m.end())
        if table not in WANT:
            continue
        drop = DROP.get(table, set())
        rows = out.setdefault(table, [])
        for r in yield_rows:
            d = dict(zip(cols, r))
            for k in drop:
                d.pop(k, None)
            rows.append(d)
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
    for t, r in out.items():
        print(t, len(r))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
