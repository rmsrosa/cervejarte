"""Build a static, read-only copy of the WordPress blog from parsed SQL JSON.

Usage: python3 -I build_site.py data.json uploads_dir out_dir
  data.json   : output of parse_sql.py (emails/IPs/user-agents already dropped)
  uploads_dir : extracted wp-content/uploads (media only; no PHP is executed)
  out_dir     : destination for the GitHub Pages site
"""
import html, json, os, re, shutil, sys
from collections import defaultdict
from urllib.parse import quote

SITE_HOST = "cervejarte.org"
BASE = "/blog"  # the blog's original address was http://cervejarte.org/blog/
OLD_BASES = ["http://cervejarte.org/blog", "https://cervejarte.org/blog",
             "http://www.cervejarte.org/blog", "https://www.cervejarte.org/blog",
             "http://blog.cervejarte.org", "https://blog.cervejarte.org"]
PER_PAGE = 5
# Calculators that live inside posts, added to a page's submenu:
# page slug -> [(post ID, form id to jump to, menu label)]
MENU_EXTRAS = {
    "calcularte": [
        ("22", "speisecalculation", "Volume de speise para carbonatação"),
        ("36", "dilutioncalculation", "Diluição de iodofor"),
        ("37", "decoctioncalculation", "Volume de mosto na decocção"),
    ],
}
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
esc = html.escape


# ---------------------------------------------------------------- wpautop port
BLOCK = ("table|thead|tfoot|caption|col|colgroup|tbody|tr|td|th|div|dl|dd|dt|ul|ol|li|pre|"
         "form|map|area|blockquote|address|math|style|p|h[1-6]|hr|fieldset|legend|section|"
         "article|aside|hgroup|header|footer|nav|figure|figcaption|details|menu|summary")


def wpautop(pee, br=True):
    if not pee.strip():
        return ""
    pre_tags = {}
    pee = pee + "\n"
    if "<pre" in pee:
        parts = pee.split("</pre>")
        last = parts.pop()
        out = ""
        for i, part in enumerate(parts):
            start = part.find("<pre")
            if start == -1:
                out += part
                continue
            name = "<pre wp-pre-tag-%d></pre>" % i
            pre_tags[name] = part[start:] + "</pre>"
            out += part[:start] + name
        pee = out + last
    pee = re.sub(r"<br\s*/?>\s*<br\s*/?>", "\n\n", pee)
    pee = re.sub(r"(<(?:%s)[\s/>])" % BLOCK, r"\n\n\1", pee)
    pee = re.sub(r"(</(?:%s)>)" % BLOCK, r"\1\n\n", pee)
    pee = re.sub(r"<hr\s*?/?>", "<hr />\n\n", pee)
    pee = pee.replace("\r\n", "\n").replace("\r", "\n")
    pee = re.sub(r"\s*<option", "<option", pee)
    pee = re.sub(r"</option>\s*", "</option>", pee)
    pee = re.sub(r"\n\n+", "\n\n", pee)
    pees = [p for p in re.split(r"\n\s*\n", pee) if p.strip()]
    pee = "".join("<p>" + p.strip("\n") + "</p>\n" for p in pees)
    pee = re.sub(r"<p>\s*</p>", "", pee)
    pee = re.sub(r"<p>([^<]+)</(div|address|form)>", r"<p>\1</p></\2>", pee)
    pee = re.sub(r"<p>\s*(</?(?:%s)[^>]*>)\s*</p>" % BLOCK, r"\1", pee)
    pee = re.sub(r"<p>(<li.+?)</p>", r"\1", pee)
    pee = re.sub(r"<p><blockquote([^>]*)>", r"<blockquote\1><p>", pee, flags=re.I)
    pee = pee.replace("</blockquote></p>", "</p></blockquote>")
    pee = re.sub(r"<p>\s*(</?(?:%s)[^>]*>)" % BLOCK, r"\1", pee)
    pee = re.sub(r"(</?(?:%s)[^>]*>)\s*</p>" % BLOCK, r"\1", pee)
    if br:
        pee = re.sub(r"<(script|style).*?</\1>",
                     lambda m: m.group(0).replace("\n", "<WPPreserveNewline />"), pee, flags=re.S)
        pee = re.sub(r"(?<!<br />)\s*\n", "<br />\n", pee)
        pee = pee.replace("<WPPreserveNewline />", "\n")
    pee = re.sub(r"(</?(?:%s)[^>]*>)\s*<br />" % BLOCK, r"\1", pee)
    pee = re.sub(r"<br />(\s*</?(?:p|li|div|dl|dd|dt|th|pre|td|ul|ol)[^>]*>)", r"\1", pee)
    pee = re.sub(r"\n</p>$", "</p>", pee)
    for k, v in pre_tags.items():
        pee = pee.replace(k, v)
    return pee


def caption_shortcode(text):
    def repl(m):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        inner = m.group(2)
        cap = attrs.get("caption", "")
        if not cap:  # newer syntax: caption text after the <img>
            mm = re.match(r"(.*?<img[^>]*>(?:</a>)?)(.*)", inner, re.S)
            if mm:
                inner, cap = mm.group(1), mm.group(2).strip()
        w = attrs.get("width", "")
        style = ' style="width:%dpx"' % (int(w) + 10) if w.isdigit() else ""
        return ('<div class="wp-caption %s"%s>%s<p class="wp-caption-text">%s</p></div>'
                % (esc(attrs.get("align", "alignnone")), style, inner.strip(), cap))
    return re.sub(r"\[caption([^\]]*)\](.*?)\[/caption\]", repl, text, flags=re.S)


def make_clickable(text):
    return re.sub(r'(?<![="\'>/])(https?://[^\s<>"\']+)',
                  lambda m: '<a href="%s" rel="nofollow ugc">%s</a>' % (m.group(1), m.group(1)), text)


# ----------------------------------------------------------------- data helpers
def php_str(serialized, key):
    m = re.search(r's:\d+:"%s";s:\d+:"(.*?)";' % re.escape(key), serialized, re.S)
    return m.group(1) if m else None


def fmt_date(dt):
    y, mo, d = dt[:10].split("-")
    return "%d de %s de %s" % (int(d), MESES[int(mo) - 1], y)


def fmt_time(dt):
    return dt[11:16]


class Site:
    def __init__(self, data):
        self.opts = {o["option_name"]: o["option_value"] for o in data["wp_options"]}
        self.name = self.opts.get("blogname", "")
        self.desc = self.opts.get("blogdescription", "")
        self.authors = {u["ID"]: u["display_name"] for u in data.get("wp_users", [])}
        posts = data["wp_posts"]
        self.by_id = {p["ID"]: p for p in posts}
        ok = lambda p: p["post_status"] == "publish" and not p["post_password"]
        self.posts = sorted([p for p in posts if p["post_type"] == "post" and ok(p)],
                            key=lambda p: p["post_date"], reverse=True)
        self.pages = sorted([p for p in posts if p["post_type"] == "page" and ok(p)],
                            key=lambda p: (int(p["menu_order"]), p["post_title"]))
        self.restored = []
        self.meta = defaultdict(dict)
        for m in data["wp_postmeta"]:
            self.meta[m["post_id"]][m["meta_key"]] = m["meta_value"]
        # taxonomy
        terms = {t["term_id"]: t for t in data["wp_terms"]}
        self.tax = {}
        for tt in data["wp_term_taxonomy"]:
            t = terms[tt["term_id"]]
            self.tax[tt["term_taxonomy_id"]] = dict(tt, name=t["name"], slug=t["slug"])
        self.post_terms = defaultdict(list)
        self.term_posts = defaultdict(list)
        live = {p["ID"] for p in self.posts}
        for r in data["wp_term_relationships"]:
            tt = self.tax.get(r["term_taxonomy_id"])
            if tt and r["object_id"] in live and tt["taxonomy"] in ("category", "post_tag"):
                self.post_terms[r["object_id"]].append(tt)
                self.term_posts[tt["term_taxonomy_id"]].append(self.by_id[r["object_id"]])
        for v in self.term_posts.values():
            v.sort(key=lambda p: p["post_date"], reverse=True)
        # comments: approved only, real comments + pingbacks/trackbacks
        self.comments = defaultdict(list)
        for c in data["wp_comments"]:
            if c["comment_approved"] == "1" and c["comment_post_ID"] in self.by_id:
                self.comments[c["comment_post_ID"]].append(c)
        for v in self.comments.values():
            v.sort(key=lambda c: (c["comment_date_gmt"], int(c["comment_ID"])))

    # ---- URLs (WordPress permalink structure /%year%/%monthnum%/%day%/%postname%/)
    def post_path(self, p):
        if p["post_type"] == "page":
            parts, q = [], p
            while q:
                parts.insert(0, q["post_name"])
                q = self.by_id.get(q["post_parent"]) if q["post_parent"] != "0" else None
            return "/" + "/".join(parts) + "/"
        y, m, d = p["post_date"][:10].split("-")
        return "/%s/%s/%s/%s/" % (y, m, d, p["post_name"])

    def term_path(self, tt):
        if tt["taxonomy"] == "post_tag":
            return "/tag/%s/" % tt["slug"]
        parts, q = [], tt
        while q:
            parts.insert(0, q["slug"])
            q = self.tax.get(q["parent"]) if q["parent"] != "0" else None
        return "/category/" + "/".join(parts) + "/"

    def url(self, path):
        return BASE + quote(path, safe="/#?=&%")

    def fix_links(self, content):
        for old in OLD_BASES:
            content = content.replace(old + "/", BASE + "/").replace(old + '"', BASE + '/"')
        # ?p=ID / ?page_id=ID style links
        def q(m):
            p = self.by_id.get(m.group(2))
            return self.url(self.post_path(p)) if p and p in self.posts + self.pages else m.group(0)
        return re.sub(r'%s/(?:index\.php)?\?(p|page_id)=(\d+)' % re.escape(BASE), q, content)

    def restore_forms(self, p):
        """Put back calculator forms whose JavaScript was stripped by a later edit
        (WordPress's visual editor removes onChange attributes). Uses the most
        recent revision where the form still had them, keeping the current labels."""
        c = p["post_content"]
        form_re = r'<form[^>]*id="%s"[^>]*>.*?</form>'
        for fid in re.findall(r'<form[^>]*id="(\w+)"', c):
            cur = re.search(form_re % fid, c, re.S)
            if not cur or re.search(r"(?i)onchange", cur.group(0)):
                continue
            revs = sorted((r for r in self.by_id.values()
                           if r["post_type"] == "revision" and r["post_parent"] == p["ID"]),
                          key=lambda r: r["post_modified"], reverse=True)
            for r in revs:
                old = re.search(form_re % fid, r["post_content"], re.S)
                if old and re.search(r"(?i)onchange", old.group(0)):
                    form = old.group(0)
                    # current label for each input name: text in the <th> before it
                    labels = {m.group(2): re.sub(r"\s+", " ", m.group(1)).strip()
                              for m in re.finditer(r'<th[^>]*>([^<]*?)\s*<input[^>]*name="(\w+)"', cur.group(0))}
                    form = re.sub(r'(<th[^>]*>)([^<]*?)(\s*<td>\s*<input[^>]*name="(\w+)")',
                                  lambda m: m.group(1) + " " + labels.get(m.group(4), m.group(2).strip()) + m.group(3), form)
                    # typo in the original: temperature fields labelled with unit "L"
                    form = re.sub(r'(<input[^>]*name="temperatura\w*"[^>]*>)L\b', r"\1C", form)
                    c = c.replace(cur.group(0), form)
                    self.restored.append((p["post_title"], fid, r["post_modified"]))
                    break
        return c

    def render_content(self, p):
        c = self.restore_forms(p)
        if "<!--photopress_album-->" in c:
            c = c.replace("<!--photopress_album-->", self.album_html())
        c = self.fix_links(caption_shortcode(c))
        return wpautop(c)


# ------------------------------------------------------------------- templates
# Styles live in tools/style.css (copied to site/assets/style.css).


class Builder:
    def __init__(self, site, out):
        self.s, self.out = site, out
        self.sidebar = self.make_sidebar()

    def write(self, path, body, title=None, canonical=None):
        dest = os.path.join(self.out, path.strip("/"), "index.html") if path.endswith("/") \
            else os.path.join(self.out, path.lstrip("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        s = self.s
        full_title = "%s | %s" % (title, s.name) if title else "%s | %s" % (s.name, s.desc)
        nav = self.nav_html()
        canon = '<link rel="canonical" href="https://%s%s">' % (SITE_HOST, s.url(canonical)) if canonical else ""
        doc = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(full_title)}</title>{canon}
<link rel="alternate" type="application/rss+xml" title="{esc(s.name)}" href="{BASE}/feed.xml">
<link rel="stylesheet" href="{BASE}/assets/style.css?v={ASSET_VER}">
<meta name="theme-color" content="#2a1a0e"></head>
<body>
<header class="site"><div class="brand"><a href="{BASE}/" title="{esc(s.name)}"><img src="{BASE}/assets/cabecalho.png" width="1000" height="288" alt="Blog da Cervejarte — cerveja artesanal desde 2004 — Ricardo Rosa"></a></div>
<nav class="menu"><div class="menu-inner"><a href="{BASE}/">Início</a>{nav}</div></nav></header>
<div class="wrap"><div class="cols"><main>{body}</main><aside>{self.sidebar}</aside></div></div>
<script src="{BASE}/assets/menu.js?v={ASSET_VER}" defer></script>
<footer class="site"><div class="wrap"><p><strong>{esc(s.name)}</strong> · {esc(s.desc)}</p><p>Arquivo estático, somente leitura, dos posts de 2007 a 2014. Os comentários estão encerrados.</p></div></footer>
</body></html>
"""
        with open(dest, "w", encoding="utf-8") as f:
            f.write(doc)

    def children(self, page):
        return [c for c in self.s.pages if c["post_parent"] == page["ID"]]

    def extras(self, page):
        """(href, label, post title) for MENU_EXTRAS entries of this page that exist."""
        out = []
        for pid, anchor, label in MENU_EXTRAS.get(page["post_name"], []):
            p = self.s.by_id.get(pid)
            if p and p in self.s.posts:
                out.append((self.s.url(self.s.post_path(p)) + "#" + anchor, label, p["post_title"]))
        return out

    def nav_html(self):
        """Top-level pages; pages with children get a dropdown (as in the old theme)."""
        s, out = self.s, []
        for p in s.pages:
            if p["post_parent"] != "0":
                continue
            kids, extra = self.children(p), self.extras(p)
            link = '<a href="%s">%s</a>' % (s.url(s.post_path(p)), esc(p["post_title"]))
            if not kids and not extra:
                out.append(link)
                continue
            items = '<a href="%s">Visão geral</a>' % s.url(s.post_path(p)) + "".join(
                '<a href="%s">%s</a>' % (s.url(s.post_path(k)), esc(k["post_title"])) for k in kids)
            if extra:
                items += '<span class="sublabel">Em posts do blog</span>' + "".join(
                    '<a href="%s">%s</a>' % (h, esc(lbl)) for h, lbl, _ in extra)
            out.append('<details class="sub"><summary>%s</summary><div class="submenu">%s</div></details>'
                       % (esc(p["post_title"]), items))
        return "".join(out)

    def make_sidebar(self):
        s = self.s
        recent = "".join('<li><a href="%s">%s</a></li>' % (s.url(s.post_path(p)), esc(p["post_title"]))
                         for p in s.posts[:8])
        cats = [t for t in s.tax.values() if t["taxonomy"] == "category" and s.term_posts[t["term_taxonomy_id"]]
                or (t["taxonomy"] == "category" and any(s.term_posts[c["term_taxonomy_id"]] for c in s.tax.values() if c["parent"] == t["term_taxonomy_id"]))]

        def tree(parent):
            items = sorted([t for t in cats if t["parent"] == parent], key=lambda t: t["name"].lower())
            if not items:
                return ""
            return "<ul>" + "".join('<li><a href="%s">%s</a> (%d)%s</li>' % (
                s.url(s.term_path(t)), esc(t["name"]), len(s.term_posts[t["term_taxonomy_id"]]),
                tree(t["term_taxonomy_id"])) for t in items) + "</ul>"
        tags = [t for t in s.tax.values() if t["taxonomy"] == "post_tag" and s.term_posts[t["term_taxonomy_id"]]]
        tag_html = ""
        if tags:
            tag_html = "<h3>Tags</h3><ul>" + "".join('<li><a href="%s">%s</a></li>' % (
                s.url(s.term_path(t)), esc(t["name"])) for t in sorted(tags, key=lambda t: t["name"].lower())) + "</ul>"
        months = defaultdict(int)
        for p in s.posts:
            months[p["post_date"][:7]] += 1
        arch = "".join('<li><a href="%s/%s/%s/">%s de %s</a> (%d)</li>' % (
            BASE, k[:4], k[5:7], MESES[int(k[5:7]) - 1].capitalize(), k[:4], n)
            for k, n in sorted(months.items(), reverse=True))
        if tag_html:
            tag_html = '<section class="widget">%s</section>' % tag_html
        return ('<section class="widget"><h3>Posts recentes</h3><ul>%s</ul></section>'
                '<section class="widget"><h3>Categorias</h3>%s</section>%s'
                '<section class="widget"><h3>Arquivos</h3><ul class="archives">%s</ul></section>' % (recent, tree("0"), tag_html, arch))

    def post_meta(self, p):
        s = self.s
        author = esc(s.authors.get(p["post_author"], ""))
        cats = [t for t in s.post_terms[p["ID"]] if t["taxonomy"] == "category"]
        tags = [t for t in s.post_terms[p["ID"]] if t["taxonomy"] == "post_tag"]
        links = lambda ts: " ".join('<a class="chip" href="%s" rel="category tag">%s</a>' % (s.url(s.term_path(t)), esc(t["name"])) for t in ts)
        n = len(s.comments[p["ID"]])
        bits = ['<time datetime="%s">%s</time>' % (p["post_date"].replace(" ", "T"), fmt_date(p["post_date"]))]
        if author:
            bits.append("por " + author)
        if cats:
            bits.append(links(cats))
        if tags:
            bits.append(links(tags))
        if n:
            bits.append('<a href="%s#comments">%d comentário%s</a>' % (s.url(s.post_path(p)), n, "" if n == 1 else "s"))
        return '<div class="meta">%s</div>' % " ".join('<span>%s</span>' % b for b in bits)

    def comment_html(self, c):
        s = self.s
        author = esc(c["comment_author"] or "Anônimo")
        url = c.get("comment_author_url", "")
        if url and re.match(r"https?://", url) and url not in ("http://", "https://"):
            author = '<a href="%s" rel="external nofollow ugc">%s</a>' % (esc(s.fix_links(url)), author)
        body = EMAIL_RE.sub("[e-mail removido]", c["comment_content"])
        if c["comment_type"] in ("pingback", "trackback"):
            return ('<div class="comment" id="comment-%s"><div class="who">%s: %s</div><div class="when">%s</div></div>'
                    % (c["comment_ID"], c["comment_type"].capitalize(), author, fmt_date(c["comment_date"])))
        body = wpautop(make_clickable(self.sanitize(body)))
        body = re.sub(r'href="([^"]*)"', lambda m: 'href="%s"' % s.fix_links(m.group(1) + '"')[:-1], body)
        initials = "".join(w[0] for w in (c["comment_author"] or "?").split()[:2]).upper() or "?"
        hue = sum(map(ord, c["comment_author"] or "?")) % 360
        own = " by-author" if c.get("user_id") not in (None, "0", "") else ""
        return ('<div class="comment%s" id="comment-%s"><div class="avatar" style="--h:%d" aria-hidden="true">%s</div>'
                '<div class="cbody"><div class="who">%s <a class="when" href="#comment-%s">%s às %s</a></div>%s</div></div>'
                % (own, c["comment_ID"], hue, esc(initials), author, c["comment_ID"], fmt_date(c["comment_date"]), fmt_time(c["comment_date"]), body))

    @staticmethod
    def sanitize(text):
        """Allow the small HTML subset WordPress permits in comments; escape everything else."""
        allowed = {"a", "abbr", "acronym", "b", "blockquote", "cite", "code", "del", "em", "i", "q", "s", "strike", "strong"}
        out, pos = [], 0
        for m in re.finditer(r"<(/?)([a-zA-Z]+)([^>]*)>", text):
            out.append(esc(text[pos:m.start()], quote=False))
            tag = m.group(2).lower()
            if tag in allowed:
                if m.group(1):
                    out.append("</%s>" % tag)
                elif tag == "a":
                    href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', m.group(3))
                    h = href.group(1) if href and re.match(r"https?://", href.group(1)) else "#"
                    out.append('<a href="%s" rel="nofollow ugc">' % esc(h))
                else:
                    out.append("<%s>" % tag)
            else:
                out.append(esc(m.group(0), quote=False))
            pos = m.end()
        out.append(esc(text[pos:], quote=False))
        return "".join(out)

    def comments_block(self, p):
        cs = self.s.comments[p["ID"]]
        kids = defaultdict(list)
        ids = {c["comment_ID"] for c in cs}
        for c in cs:
            kids[c["comment_parent"] if c["comment_parent"] in ids else "0"].append(c)

        def tree(parent):
            return "<ol>" + "".join("<li>%s%s</li>" % (self.comment_html(c), tree(c["comment_ID"]) if kids[c["comment_ID"]] else "")
                                    for c in kids[parent]) + "</ol>"
        head = ""
        if cs:
            n = len(cs)
            head = '<h2>%d comentário%s</h2>%s' % (n, "" if n == 1 else "s", tree("0"))
        return '<section class="comments" id="comments">%s<p class="closed">Os comentários estão encerrados — este é um arquivo somente leitura.</p></section>' % head

    def article(self, p, single):
        s = self.s
        h = "h1" if single else "h2"
        title = esc(p["post_title"])
        if not single:
            title = '<a href="%s">%s</a>' % (s.url(s.post_path(p)), title)
        meta = self.post_meta(p) if p["post_type"] == "post" else ""
        extra = ""
        kids = self.children(p) if p["post_type"] == "page" else []
        more = self.extras(p) if p["post_type"] == "page" else []
        if kids or more:
            extra = '<ul class="subpages">%s%s</ul>' % ("".join(
                '<li><a href="%s">%s</a></li>' % (s.url(s.post_path(k)), esc(k["post_title"])) for k in kids), "".join(
                '<li><a href="%s">%s<small>no post “%s”</small></a></li>' % (h, esc(lbl), esc(t)) for h, lbl, t in more))
        return '<article id="post-%s"><%s class="entry">%s</%s>%s<div class="entry-content">%s%s</div></article>' % (
            p["ID"], h, title, h, meta, s.render_content(p), extra)

    def listing(self, base_path, items, heading, title):
        pages = [items[i:i + PER_PAGE] for i in range(0, len(items), PER_PAGE)] or [[]]
        for n, chunk in enumerate(pages, 1):
            path = base_path if n == 1 else "%spage/%d/" % (base_path, n)
            body = ('<header class="archive-head"><h1>%s</h1></header>' % heading if heading else "")
            body += "".join(self.article(p, False) for p in chunk)
            nav = ""
            if n < len(pages):
                nav += '<a href="%s">← Posts mais antigos</a>' % self.s.url("%spage/%d/" % (base_path, n + 1))
            else:
                nav += "<span></span>"
            if n > 1:
                prev = base_path if n == 2 else "%spage/%d/" % (base_path, n - 1)
                nav += '<a href="%s">Posts mais recentes →</a>' % self.s.url(prev)
            body += '<nav class="pager">%s</nav>' % nav
            self.write(path, body, title if n == 1 else "%s – página %d" % (title or self.s.name, n), path)

    def build(self):
        s = self.s
        for p in s.posts + s.pages:
            path = s.post_path(p)
            body = self.article(p, True)
            if p["post_type"] == "post":
                i = s.posts.index(p)
                older = s.posts[i + 1] if i + 1 < len(s.posts) else None
                newer = s.posts[i - 1] if i > 0 else None
                body += '<nav class="pager">%s%s</nav>' % (
                    '<a href="%s">← %s</a>' % (s.url(s.post_path(older)), esc(older["post_title"])) if older else "<span></span>",
                    '<a href="%s">%s →</a>' % (s.url(s.post_path(newer)), esc(newer["post_title"])) if newer else "")
            if p["comment_status"] != "closed" or s.comments[p["ID"]]:
                body += self.comments_block(p)
            self.write(path, body, p["post_title"], path)
        self.attachments()
        self.listing("/", s.posts, "", None)
        for tt in s.tax.values():
            if tt["taxonomy"] in ("category", "post_tag"):
                ps = s.term_posts[tt["term_taxonomy_id"]]
                label = "Categoria" if tt["taxonomy"] == "category" else "Tag"
                if ps:
                    self.listing(s.term_path(tt), ps, "%s: %s" % (label, esc(tt["name"])), tt["name"])
        by_y, by_m, by_d = defaultdict(list), defaultdict(list), defaultdict(list)
        for p in s.posts:
            by_y[p["post_date"][:4]].append(p)
            by_m[p["post_date"][:7]].append(p)
            by_d[p["post_date"][:10]].append(p)
        for ymd, ps in by_d.items():
            self.listing("/%s/" % ymd.replace("-", "/"), ps, "Arquivo de " + fmt_date(ymd), fmt_date(ymd))
        for y, ps in by_y.items():
            self.listing("/%s/" % y, ps, "Arquivo de %s" % y, y)
        for ym, ps in by_m.items():
            y, m = ym.split("-")
            lbl = "%s de %s" % (MESES[int(m) - 1].capitalize(), y)
            self.listing("/%s/%s/" % (y, m), ps, "Arquivo de " + lbl, lbl)
        self.write("/404.html", '<h1 class="entry">Página não encontrada</h1><p>O endereço solicitado não existe neste arquivo. '
                   'Veja os <a href="%s/">posts mais recentes</a> ou as categorias ao lado.</p>' % BASE, "Página não encontrada")
        self.feed()

    def attachments(self):
        """WordPress attachment pages: /<parent permalink>/<attachment slug>/ (and /attachment/<slug>/)."""
        s = self.s
        live = {p["ID"] for p in s.posts + s.pages}
        for a in s.by_id.values():
            if a["post_type"] != "attachment" or a["post_parent"] not in live or not a["post_name"]:
                continue
            parent = s.by_id[a["post_parent"]]
            f = s.meta[a["ID"]].get("_wp_attached_file")
            if not f:
                continue
            src = "%s/wp-content/uploads/%s" % (BASE, quote(f))
            alt = s.meta[a["ID"]].get("_wp_attachment_image_alt") or a["post_title"]
            media = ('<a href="%s"><img src="%s" alt="%s"></a>' % (src, src, esc(alt))
                     if a["post_mime_type"].startswith("image/") else '<a href="%s">%s</a>' % (src, esc(a["post_title"])))
            cap = '<p class="wp-caption-text">%s</p>' % esc(a["post_excerpt"]) if a["post_excerpt"] else ""
            body = ('<article><h1 class="entry">%s</h1><div class="meta">Publicado em <a href="%s">%s</a></div>'
                    '<div class="entry-content">%s%s%s</div></article>' % (
                        esc(a["post_title"]), s.url(s.post_path(parent)), esc(parent["post_title"]),
                        media, cap, wpautop(a["post_content"])))
            if s.comments[a["ID"]]:
                body += self.comments_block(a)
            base = s.post_path(parent)
            for path in {base + a["post_name"] + "/", base + "attachment/" + a["post_name"] + "/"}:
                self.write(path, body, a["post_title"], base + a["post_name"] + "/")

    def feed(self):
        s = self.s
        items = []
        for p in s.posts[:15]:
            link = "https://%s%s" % (SITE_HOST, s.url(s.post_path(p)))
            items.append("<item><title>%s</title><link>%s</link><guid>%s</guid><pubDate>%s</pubDate>"
                         "<description>%s</description></item>" % (
                             esc(p["post_title"]), link, link, rfc822(p["post_date_gmt"]),
                             esc(s.render_content(p).replace('src="/', 'src="https://%s/' % SITE_HOST)
                                 .replace('href="/', 'href="https://%s/' % SITE_HOST))))
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>%s</title>'
               "<link>https://%s%s/</link><description>%s</description><language>pt-BR</language>%s</channel></rss>\n"
               % (esc(s.name), SITE_HOST, BASE, esc(s.desc), "".join(items)))
        with open(os.path.join(self.out, "feed.xml"), "w", encoding="utf-8") as f:
            f.write(xml)


def rfc822(dt):
    import datetime
    d = datetime.datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
    return d.strftime("%a, %d %b %Y %H:%M:%S +0000")


def album_html(site):
    groups = defaultdict(list)
    for pid, meta in site.meta.items():
        p = site.by_id.get(pid)
        if not p or p["post_type"] != "attachment" or "pp2_catarray" not in meta:
            continue
        f = meta.get("_wp_attached_file")
        if not f:
            continue
        cat = php_str(meta["pp2_catarray"], "category") or "Outras"
        md = meta.get("_wp_attachment_metadata", "")
        thumb = re.search(r's:9:"thumbnail";a:\d+:\{s:4:"file";s:\d+:"([^"]+)"', md)
        d = os.path.dirname(f)
        thumb = (d + "/" + thumb.group(1)) if thumb else f
        groups[cat].append((p["post_date"], f, thumb, meta.get("_wp_attachment_image_alt") or p["post_title"]))
    out = []
    for cat in sorted(groups, key=lambda c: min(g[0] for g in groups[c])):
        imgs = "".join('<a href="%s/wp-content/uploads/%s"><img loading="lazy" src="%s/wp-content/uploads/%s" alt="%s"></a>'
                       % (BASE, quote(f), BASE, quote(t), esc(alt)) for _, f, t, alt in sorted(groups[cat]))
        out.append("<h3>%s</h3><div class=\"album\">%s</div>" % (esc(cat), imgs))
    return "\n".join(out)


Site.album_html = lambda self: album_html(self)


GENERATED = "<!-- generated by build_site.py -->"
# Short hash of style.css + menu.js, appended as ?v= so browsers fetch new
# versions after a rebuild instead of mixing new pages with cached old styles.
ASSET_VER = ""


def main(data_path, uploads, repo):
    """repo = root of the GitHub Pages repo for SITE_HOST. The blog is written to
    repo/<BASE>/ and only that folder is replaced; the rest of the repo (the main
    cervejarte.org site) is left alone."""
    global ASSET_VER
    import hashlib
    here = os.path.dirname(os.path.realpath(__file__))
    h = hashlib.sha1()
    for fn in ("style.css", "menu.js"):
        with open(os.path.join(here, fn), "rb") as f:
            h.update(f.read())
    ASSET_VER = h.hexdigest()[:8]
    data = json.load(open(data_path, encoding="utf-8"))
    site = Site(data)
    out = os.path.join(repo, BASE.strip("/")) if BASE else repo
    if not BASE and os.path.isdir(out):
        raise SystemExit("refusing to wipe the repo root; set BASE")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "assets"))
    here = os.path.dirname(os.path.realpath(__file__))
    shutil.copy2(os.path.join(here, "style.css"), os.path.join(out, "assets", "style.css"))
    shutil.copy2(os.path.join(here, "menu.js"), os.path.join(out, "assets", "menu.js"))
    hdr = os.path.join(uploads, "2011", "10", "cabecalho-blog-cervejarte421.png")
    if os.path.isfile(hdr):
        shutil.copy2(hdr, os.path.join(out, "assets", "cabecalho.png"))
    # Copy media only (whitelisted extensions); never anything executable.
    media = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|mp3|mp4|ogg|zip)$", re.I)
    n = 0
    for root, _, files in os.walk(uploads):
        for fn in files:
            src = os.path.join(root, fn)
            if media.search(fn) and not os.path.islink(src):
                dst = os.path.join(out, "wp-content", "uploads", os.path.relpath(src, uploads))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                n += 1
    Builder(site, out).build()

    # Repo-level files. GitHub Pages only uses the 404.html at the repo root.
    def owned(path):  # missing, or written by this script earlier
        return not os.path.exists(path) or GENERATED in open(path, encoding="utf-8").read()
    blog404, root404 = os.path.join(out, "404.html"), os.path.join(repo, "404.html")
    if BASE and owned(root404):
        with open(blog404, encoding="utf-8") as f:
            page = f.read()
        with open(root404, "w", encoding="utf-8") as f:
            f.write(page.replace("<!doctype html>", "<!doctype html>" + GENERATED, 1))
    root_index = os.path.join(repo, "index.html")
    if BASE and owned(root_index):
        with open(root_index, "w", encoding="utf-8") as f:
            f.write("""<!doctype html>%s
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cervejarte</title><meta http-equiv="refresh" content="0; url=%s/"><link rel="canonical" href="https://%s%s/"></head>
<body><p>Placeholder até o site principal da Cervejarte voltar: <a href="%s/">ir para o blog</a>.</p></body></html>
""" % (GENERATED, BASE, SITE_HOST, BASE, BASE))
    open(os.path.join(repo, ".nojekyll"), "a").close()
    cname = os.path.join(repo, "CNAME")
    if not os.path.exists(cname):
        with open(cname, "w") as f:
            f.write(SITE_HOST + "\n")
    for t, fid, when in sorted(set(site.restored)):
        print("restored form #%s in %r from revision of %s" % (fid, t, when))
    print("blog -> %s  posts=%d pages=%d comments=%d media=%d" % (
        out, len(site.posts), len(site.pages), sum(len(v) for v in site.comments.values()), n))


if __name__ == "__main__":
    main(*sys.argv[1:4])
