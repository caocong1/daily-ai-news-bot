"""arXiv cs.AI + cs.CL — last 24h of new papers."""
from ._common import fetch_url, strip_html
import time
import urllib.parse


def fetch():
    # arXiv API: 7-day window so we always have something to consider; LLM
    # filters freshness downstream (the user wants <24h content, but a slightly
    # wider net lets us not miss important papers right at the boundary).
    cutoff = time.strftime("%Y%m%d", time.gmtime(time.time() - 7 * 86400))
    now = time.strftime("%Y%m%d", time.gmtime())
    cat = "cat:cs.AI OR cat:cs.CL OR cat:cs.LG OR cat:cs.CV"
    q = f"({cat}) AND submittedDate:[{cutoff}0000 TO {now}2359]"
    params = urllib.parse.urlencode({
        "search_query": q,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": "60",
    })
    url = "http://export.arxiv.org/api/query?" + params
    xml = fetch_url(url, headers={"Accept": "application/atom+xml"})
    out = []
    import re
    for m in re.finditer(r"<entry>(.*?)</entry>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        id_m = re.search(r"<id>(.*?)</id>", block, re.DOTALL)
        pub_m = re.search(r"<published>(.*?)</published>", block)
        sum_m = re.search(r"<summary>(.*?)</summary>", block, re.DOTALL)
        if not (title_m and id_m):
            continue
        title = re.sub(r"\s+", " ", strip_html(title_m.group(1)))
        link = strip_html(id_m.group(1))
        # arXiv ID links to abs page; keep abs page for nice reading
        link = link.replace("/pdf/", "/abs/") if "/pdf/" in link else link
        pub_at = None
        if pub_m:
            try:
                from email.utils import parsedate_to_datetime
                pub_at = int(parsedate_to_datetime(pub_m.group(1).strip()).timestamp())
            except Exception:
                pass
        summary = strip_html(sum_m.group(1))[:600] if sum_m else ""
        out.append({
            "url": link, "title": title, "body": summary,
            "source": "arxiv_ai", "published_at": pub_at,
        })
    return out
