## Link specification pages on the web

When a block's specification lives on a wiki or another web site, list its pages under `links`. `docs` takes local file paths only; a URL there logs a warning to move it.

```yaml
blocks:
  - name: my_block
    desc: Brief description
    links:
      - kind: arch
        title: "Arch: flow control"
        url: https://wiki.example.com/pages/123
      - kind: mas
        url: https://wiki.example.com/pages/456
```

`kind` says what the page is, such as `arch`, `mas`, `rtl`, `dv` or `pd`, and defaults to `doc`. `title` defaults to the URL. A URL that is not absolute `http://` or `https://` fails the load. List every link, with its block, kind, title and URL:

```bash
rb --machine spec links
rb spec links --block my_block
```

Like `rb spec list`, it searches `spec/`, or `--spec-dir`. rtl-buddy never fetches the pages. The [design knowledge graph](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/#graph-data-model) holds each URL as a `spec_link` node, so a graph traversal reaches the page from the block and an agent can read it from there.
