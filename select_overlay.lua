local utils = require 'mp.utils'

local M = {}

local python_command = nil
local python_options_json = nil
local callback = nil
local fetch_anime_info = nil

function M.init(deps)
    python_command = deps.python_command
    python_options_json = deps.python_options_json
    callback = deps.callback
    fetch_anime_info = deps.fetch_anime_info
end

local overlay = {
    active = false,
    focus = 1,
    candidates = {},
    path = nil,
    s_dir = nil
}

local overlay_timer = mp.add_periodic_timer(0.1, function()
    if not overlay.active then
        return
    end

    local w, h = mp.get_osd_size()
    if not w or not h or w == 0 or h == 0 then
        return
    end

    local ass = "{\\an7\\pos(40,50)\\fs30\\bord2\\shad0}Multiple Anime Matches Found"
            .. "\\N{\\fs20}Select the correct title to save to cache:"
            .. "\\N"

    for i, c in ipairs(overlay.candidates) do
        local marker = (overlay.focus == i) and "{\\c&H00FFFF&}> " or "  "
        local year_str = (c.year and c.year ~= "Unknown") and (" (" .. c.year .. ")") or ""
        ass = ass .. "\\N{\\fs22}" .. marker .. c.name .. year_str .. "{\\c&HFFFFFF&}"
    end

    ass = ass .. "\\N"
              .. "\\N{\\fs18}Up/Down: select | Enter: submit | Esc: cancel"

    mp.set_osd_ass(w, h, ass)
end)
overlay_timer:stop()

local function clear_overlay_bindings()
    local keys = {"ESC", "ENTER", "KP_ENTER", "UP", "DOWN"}
    for _, key in ipairs(keys) do
        mp.remove_key_binding("select_overlay_" .. key)
    end
end

local function close_overlay()
    overlay.active = false
    overlay_timer:stop()
    mp.set_osd_ass(0, 0, "")
    clear_overlay_bindings()
end

local function submit_selection()
    local selected = overlay.candidates[overlay.focus]
    if not selected or not selected.id then
        return
    end

    local anime_id = selected.id
    local args = {python_command, overlay.s_dir .. "anilistUpdater.py", overlay.path, "save_cache", python_options_json, tostring(anime_id)}

    close_overlay()
    mp.osd_message("Saving selection...", 2)

    mp.command_native_async({
        name = "subprocess",
        args = args,
        capture_stdout = true
    }, function(success, result, error)
        if success and result and result.status == 0 then
            mp.osd_message("Saved to cache! Reloading info...", 2)
            if fetch_anime_info then
                fetch_anime_info()
            end
        else
            mp.osd_message("Failed to save selection.", 3)
        end
    end)
end

function M.open_select_overlay(path, s_dir, candidates)
    if not candidates or #candidates == 0 then
        return
    end

    overlay.active = true
    overlay.focus = 1
    overlay.path = path
    overlay.s_dir = s_dir
    overlay.candidates = candidates

    mp.add_forced_key_binding("ESC", "select_overlay_ESC", function()
        close_overlay()
        mp.osd_message("Anime selection cancelled. Anime will not be updated.", 3)
    end)

    mp.add_forced_key_binding("ENTER", "select_overlay_ENTER", submit_selection)
    mp.add_forced_key_binding("KP_ENTER", "select_overlay_KP_ENTER", submit_selection)

    mp.add_forced_key_binding("UP", "select_overlay_UP", function()
        overlay.focus = overlay.focus - 1
        if overlay.focus < 1 then
            overlay.focus = #overlay.candidates
        end
    end)

    mp.add_forced_key_binding("DOWN", "select_overlay_DOWN", function()
        overlay.focus = overlay.focus + 1
        if overlay.focus > #overlay.candidates then
            overlay.focus = 1
        end
    end)

    overlay_timer:resume()
end

return M
