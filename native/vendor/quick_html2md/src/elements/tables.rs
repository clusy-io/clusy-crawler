//! GFM table conversion.

use dom_query::Selection;

/// Minimum column width for table cells.
const MIN_COLUMN_WIDTH: usize = 3;

/// Column alignment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Alignment {
    None,
    Left,
    Center,
    Right,
}

impl Alignment {
    /// Parse alignment from HTML align attribute.
    pub fn from_attr(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "left" => Self::Left,
            "center" => Self::Center,
            "right" => Self::Right,
            _ => Self::None,
        }
    }

    /// Generate separator string for this alignment.
    pub fn separator(&self, width: usize) -> String {
        let w = width.max(MIN_COLUMN_WIDTH);
        match self {
            Self::Left => format!(":{}", "-".repeat(w - 1)),
            Self::Center => format!(":{}:", "-".repeat(w.saturating_sub(2))),
            Self::Right => format!("{}:", "-".repeat(w - 1)),
            Self::None => "-".repeat(w),
        }
    }
}

/// Convert HTML table to GFM markdown table.
pub(crate) fn convert_table(table: &Selection, output: &mut String) {
    let mut rows: Vec<Vec<String>> = Vec::new();
    let mut alignments: Vec<Alignment> = Vec::new();

    // Extract header from <thead> if present
    let thead = table.select("thead tr");
    if thead.exists() {
        for tr in thead.iter() {
            let mut row = Vec::new();
            for th in tr.select("th").iter() {
                let text = th.text().trim().to_string();
                let align = th
                    .attr("align")
                    .map(|a| Alignment::from_attr(&a))
                    .unwrap_or(Alignment::None);
                alignments.push(align);
                // Escape pipe characters in header cells
                row.push(escape_pipe(&text));
            }
            if !row.is_empty() {
                rows.push(row);
            }
        }
    }

    // Extract body rows
    let tbody_rows = table.select("tbody tr, table > tr");
    for tr in tbody_rows.iter() {
        let mut row = Vec::new();
        let cells = tr.select("td, th");
        for (i, cell) in cells.iter().enumerate() {
            let text = cell.text().trim().to_string();

            // Capture alignment from first data row if no header
            if rows.is_empty() || (rows.len() == 1 && i >= alignments.len()) {
                let align = cell
                    .attr("align")
                    .map(|a| Alignment::from_attr(&a))
                    .unwrap_or(Alignment::None);
                if i >= alignments.len() {
                    alignments.push(align);
                }
            }

            row.push(escape_pipe(&text));
        }
        if !row.is_empty() {
            rows.push(row);
        }
    }

    if rows.is_empty() {
        return;
    }

    // Calculate column widths
    let col_count = rows.iter().map(|r| r.len()).max().unwrap_or(0);
    let mut widths: Vec<usize> = vec![MIN_COLUMN_WIDTH; col_count];

    for row in &rows {
        for (i, cell) in row.iter().enumerate() {
            if i < widths.len() {
                widths[i] = widths[i].max(cell.chars().count());
            }
        }
    }

    // Ensure alignments vector matches column count
    while alignments.len() < col_count {
        alignments.push(Alignment::None);
    }

    // Build output
    for (row_idx, row) in rows.iter().enumerate() {
        output.push('|');
        for (col_idx, cell) in row.iter().enumerate() {
            let width = widths.get(col_idx).copied().unwrap_or(MIN_COLUMN_WIDTH);
            output.push(' ');
            output.push_str(&pad_cell(cell, width, alignments[col_idx]));
            output.push_str(" |");
        }
        // Pad missing cells
        for col_idx in row.len()..col_count {
            let width = widths.get(col_idx).copied().unwrap_or(MIN_COLUMN_WIDTH);
            output.push(' ');
            output.push_str(&" ".repeat(width));
            output.push_str(" |");
        }
        output.push('\n');

        // Add separator after first row (header)
        if row_idx == 0 {
            output.push('|');
            for (col_idx, alignment) in alignments.iter().enumerate().take(col_count) {
                let width = widths.get(col_idx).copied().unwrap_or(MIN_COLUMN_WIDTH);
                output.push(' ');
                output.push_str(&alignment.separator(width));
                output.push_str(" |");
            }
            output.push('\n');
        }
    }
}

/// Escape pipe characters in table cell content.
fn escape_pipe(text: &str) -> String {
    text.replace('|', r"\|")
}

/// Pad cell content to specified width based on alignment.
fn pad_cell(text: &str, width: usize, align: Alignment) -> String {
    let len = text.chars().count();
    if len >= width {
        return text.to_string();
    }

    let padding = width - len;
    match align {
        Alignment::Right => format!("{}{}", " ".repeat(padding), text),
        Alignment::Center => {
            let left = padding / 2;
            let right = padding - left;
            format!("{}{}{}", " ".repeat(left), text, " ".repeat(right))
        }
        _ => format!("{}{}", text, " ".repeat(padding)),
    }
}
