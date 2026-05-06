-- rtl-buddy wave annotation plugin
-- Installed by: rb wave-install-nvim
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

-- <leader>wa — send word under cursor to Surfer via the wave control socket.
-- Requires ctrl-sock to be configured in cfg-surfer (root_config.yaml).
-- Path is passed via WAVE_CTRL_SOCK env var set by rb wave on launch.
local _ctrl_sock = vim.fn.getenv("WAVE_CTRL_SOCK")

local function wave_add_variable()
  local name = vim.fn.expand("<cword>")
  if name == "" then return end
  local pipe = vim.uv.new_pipe(false)
  pipe:connect(_ctrl_sock, function(err)
    if err then
      vim.schedule(function()
        vim.notify("rb wave: ctrl-sock unavailable — is rb wave running? (" .. err .. ")", vim.log.levels.WARN)
      end)
      return
    end
    local msg = vim.json.encode({ cmd = "add_variable", name = name }) .. "\n"
    pipe:write(msg, function() pipe:close() end)
  end)
end

vim.keymap.set("n", "<leader>wa", wave_add_variable,
  { desc = "wave: add signal under cursor to Surfer waveform" })

return M
