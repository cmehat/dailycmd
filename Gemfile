source "https://rubygems.org"

# Native GitHub Pages build: this gem pins Jekyll + the allow-listed plugins to
# exactly what GitHub runs server-side, so a plain `git push` rebuilds the site
# with no GitHub Actions workflow required.
gem "github-pages", group: :jekyll_plugins

# Needed only for local preview (`bundle exec jekyll serve`) on Ruby 3+.
gem "webrick", "~> 1.8"
