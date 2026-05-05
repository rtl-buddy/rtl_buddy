-- rtl-buddy wave annotation plugin
-- Installed by: rb wave install-nvim
-- Source: https://rtl-buddy.github.io/rtl_buddy/

local M = {}

local function set_hl()
  vim.api.nvim_set_hl(0, "WaveValue", { fg = "#000000", bg = "#fffacd", bold = true })
end

set_hl()
-- Reapply after colorscheme changes (highlight clear wipes custom groups)
vim.api.nvim_create_autocmd("ColorScheme", { callback = set_hl })

-- On first launch via rb wave, nvim is opened with WAVE_VALUE env var set.
-- Show the selected signal value as virtual text at the declaration line.
vim.api.nvim_create_autocmd("VimEnter", {
  callback = function()
    local value = vim.fn.getenv("WAVE_VALUE")
    if value == vim.NIL or value == "" then return end
    vim.schedule(function()
      local ns = vim.api.nvim_create_namespace("wave_value")
      local line = vim.fn.line(".") - 1
      vim.api.nvim_buf_set_extmark(0, ns, line, 0, {
        virt_text = {{ "▶ " .. value, "WaveValue" }},
        virt_text_pos = "eol",
      })
    end)
  end,
})

return M
