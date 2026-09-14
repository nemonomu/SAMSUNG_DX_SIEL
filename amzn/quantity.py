"""Strict remaining-stock text shared by Amazon listing capture and DB merge."""
import json
import re


LISTING_QUANTITY_PRODUCTS = frozenset({'tv', 'ref', 'ldy'})
_QUANTITY_PATTERN = r'Only\s+[1-9][0-9]*\s+left\s+in\s+stock\.?'
_QUANTITY_RE = re.compile(_QUANTITY_PATTERN, re.IGNORECASE)


def quantity_text(value):
    """Accept the whole stock statement, never numbers from other messages."""
    if isinstance(value, str) and _QUANTITY_RE.fullmatch(value.strip()):
        return value
    return None


# A card-local function used by both Selenium main cards and the BSR JS pass.
# Whitespace is normalized during capture; wording, case and punctuation remain.
LISTING_QUANTITY_JS = r"""(card) => {
    const clean = value => (value || '').replace(/\s+/g, ' ').trim();
    const pattern = __QUANTITY_PATTERN__;
    const exact = new RegExp('^' + pattern + '$', 'i');
    const line = new RegExp('(?:^|\\n)[ \\t]*(' + pattern + ')[ \\t]*(?=\\n|$)', 'gi');
    const styleCache = new WeakMap();
    const visibleCache = new WeakMap();
    const textCache = new WeakMap();
    const styleOf = el => {
        if (!styleCache.has(el)) styleCache.set(el, getComputedStyle(el));
        return styleCache.get(el);
    };
    const clippedAway = (el, style) => {
        const rect = style.clip.match(/^rect\(([^)]+)\)$/);
        if (rect) {
            const edges = rect[1].split(/[,\s]+/).filter(Boolean).map(parseFloat);
            if (edges.length === 4 && edges.every(Number.isFinite) &&
                (edges[2] <= edges[0] || edges[1] <= edges[3])) return true;
        }
        const inset = style.clipPath.match(/^inset\(([^)]+)\)$/);
        if (inset) {
            const parts = inset[1].split(/\s+round\s+/)[0].trim().split(/\s+/);
            if (parts.length <= 4 && parts.every(p => /^-?[\d.]+(?:px|%)$/.test(p))) {
                const [top, right = top, bottom = top, left = right] = parts;
                const box = el.getBoundingClientRect();
                const pixels = (value, size) => parseFloat(value) * (value.endsWith('%') ? size / 100 : 1);
                if (pixels(top, box.height) + pixels(bottom, box.height) >= box.height ||
                    pixels(left, box.width) + pixels(right, box.width) >= box.width) return true;
            }
        }
        return false;
    };
    const isVisible = el => {
        if (!el) return true;
        if (visibleCache.has(el)) return visibleCache.get(el);
        const style = styleOf(el);
        const visible = !el.matches('[hidden], [aria-hidden="true"], .a-offscreen') &&
            style.display !== 'none' && style.contentVisibility !== 'hidden' && Number(style.opacity) !== 0 &&
            !clippedAway(el, style) && isVisible(el.parentElement);
        visibleCache.set(el, visible);
        return visible;
    };
    const visibleTextOf = el => {
        if (textCache.has(el)) return textCache.get(el);
        if (!isVisible(el)) { textCache.set(el, ''); return ''; }
        if (el.tagName === 'BR') return '\n';
        let value = '';
        for (const child of el.childNodes) {
            if (child.nodeType === Node.TEXT_NODE) {
                const style = styleOf(el);
                // These properties can be overridden by children, so filter text
                // nodes individually rather than discarding the whole subtree.
                if (style.visibility === 'hidden' || style.visibility === 'collapse' ||
                    parseFloat(style.fontSize) === 0) continue;
                const whitespace = style.whiteSpace;
                value += /^(pre|pre-wrap|break-spaces)$/.test(whitespace) ? child.nodeValue :
                    whitespace === 'pre-line' ? child.nodeValue.replace(/[^\S\n]+/g, ' ') :
                    child.nodeValue.replace(/\s+/g, ' ');
            } else if (child.nodeType === Node.ELEMENT_NODE) {
                value += visibleTextOf(child);
            }
        }
        // Preserve block boundaries, but collapse source-code newlines in inline text.
        const display = styleOf(el).display;
        if (!display.startsWith('inline') && display !== 'contents') value = '\n' + value + '\n';
        textCache.set(el, value);
        return value;
    };
    const visibleText = visibleTextOf(card);
    const statements = new Set(Array.from(visibleText.matchAll(line),
        match => clean(match[1]).toLowerCase()));
    if (!statements.size) return null;
    const asinOf = node => node.getAttribute('data-asin') ||
        ((node.querySelector('a[href*="/dp/"], a[href*="/gp/product/"]')?.href || '')
            .match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1];
    const cardAsin = asinOf(card);
    const found = new Map();
    for (const el of card.querySelectorAll('span, div')) {
        if (!isVisible(el)) continue;
        // Product titles, recommendations and variant cards are not stock notices.
        if (el.closest('h1, h2, h3, a[href*="/dp/"], a[href*="/gp/product/"], '
            + '.a-carousel-container, .a-carousel-card, [data-a-carousel-options]')) continue;
        let foreignCard = false;
        for (let parent = el; parent && parent !== card; parent = parent.parentElement) {
            const asin = parent.getAttribute('data-asin');
            if (asin && asin !== cardAsin) { foreignCard = true; break; }
            if (parent.matches('.zg-grid-general-faceout') && asinOf(parent) !== cardAsin) {
                foreignCard = true; break;
            }
            if (parent.matches('[data-component-type="s-search-result"], #gridItemRoot')) {
                foreignCard = true; break;
            }
        }
        if (foreignCard) continue;
        // Reject wrappers containing another product, even if its only text is stock.
        if (Array.from(el.querySelectorAll('[data-asin], .zg-grid-general-faceout')).some(
            child => asinOf(child) && asinOf(child) !== cardAsin
        ) || el.querySelector('[data-component-type="s-search-result"], #gridItemRoot, '
            + '.a-carousel-container, .a-carousel-card, h1, h2, h3, '
            + 'a[href*="/dp/"], a[href*="/gp/product/"]')) continue;
        const value = clean(visibleTextOf(el));
        if (exact.test(value) && statements.has(value.toLowerCase())) {
            found.set(value.toLowerCase().replace(/\.$/, ''), value);
        }
    }
    // Conflicting stock statements inside one card are ambiguous: leave NULL.
    return found.size === 1 ? found.values().next().value : null;
}""".replace('__QUANTITY_PATTERN__', json.dumps(_QUANTITY_PATTERN))
