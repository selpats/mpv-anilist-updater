--[[
Configuration options for anilistUpdater (set in anilistUpdater.conf):

DIRECTORIES: Table or comma/semicolon-separated string. The directories the script will work on. Leaving it empty will make it work on every video you watch with mpv. Example: DIRECTORIES = {"D:/Torrents", "D:/Anime"}

EXCLUDED_DIRECTORIES: Table or comma/semicolon-separated string. Useful for ignoring paths inside directories from above. Example: EXCLUDED_DIRECTORIES = {"D:/Torrents/Watched", "D:/Anime/Planned"}

UPDATE_PERCENTAGE: Number (0-100). The percentage of the video you need to watch before it updates AniList automatically. Default is 85 (usually before the ED of a usual episode duration).

SET_COMPLETED_TO_REWATCHING_ON_FIRST_EPISODE: Boolean. If true, when watching episode 1 of a completed anime, set it to rewatching and update progress.

UPDATE_PROGRESS_WHEN_REWATCHING: Boolean. If true, allow updating progress for anime set to rewatching. This is for if you want to set anime to rewatching manually, but still update progress automatically.

SET_TO_COMPLETED_AFTER_LAST_EPISODE_CURRENT: Boolean. If true, set to COMPLETED after last episode if status was CURRENT.

SET_TO_COMPLETED_AFTER_LAST_EPISODE_REWATCHING: Boolean. If true, set to COMPLETED after last episode if status was REPEATING (rewatching).

ADD_ENTRY_IF_MISSING: Boolean. If true, automatically add anime to your list if it's not found during search. Default is false.

CACHE_REFRESH_RATE: Number (hours). How long a normal cache entry stays valid. Default is 24.

CACHE_MODE: String. Either NORMAL or SLIDING. NORMAL keeps current behavior. SLIDING refreshes cache TTL on reads for all titles.

SILENT_MODE: Boolean. If true, won't show OSD messages.
]]

local utils = require 'mp.utils'
local mpoptions = require("mp.options")
local correction_overlay = require("correction_overlay")

local conf_name = "anilistUpdater.conf"
local script_dir = (debug.getinfo(1).source:match("@?(.*/)") or "./")

-- Helper function to get MPV config directory
local function get_mpv_config_dir()
    return os.getenv("APPDATA") and utils.join_path(os.getenv("APPDATA"), "mpv") or 
           os.getenv("HOME") and utils.join_path(utils.join_path(os.getenv("HOME"), ".config"), "mpv") or nil
end

-- Helper function to normalize path separators
local function normalize_path(p)
    p = p:gsub("\\", "/")
    if p:sub(-1) == "/" then
        p = p:sub(1, -2)
    end
    return p
end

-- Helper function to parse directory strings (comma or semicolon separated)
local function parse_directory_string(dir_string)
    if type(dir_string) == "string" and dir_string ~= "" then
        local dirs = {}
        for dir in string.gmatch(dir_string, "([^,;]+)") do
            local trimmed = (dir:gsub("^%s*(.-)%s*$", "%1"):gsub('[\'"]', '')) -- trim
            table.insert(dirs, normalize_path(trimmed))
        end
        return dirs
    else
        return {}
    end
end

-- Default config
local default_conf = [[# anilistUpdater Configuration
# For detailed explanations of all available options, see:
# https://github.com/AzuredBlue/mpv-anilist-updater?tab=readme-ov-file#configuration-anilistupdaterconf

# Use 'yes' or 'no' for boolean options below
# Example for multiple directories (comma or semicolon separated):
# DIRECTORIES=D:/Torrents,D:/Anime
# or
# DIRECTORIES=D:/Torrents;D:/Anime
DIRECTORIES=
EXCLUDED_DIRECTORIES=
UPDATE_PERCENTAGE=85
SET_COMPLETED_TO_REWATCHING_ON_FIRST_EPISODE=no
UPDATE_PROGRESS_WHEN_REWATCHING=yes
SET_TO_COMPLETED_AFTER_LAST_EPISODE_CURRENT=yes
SET_TO_COMPLETED_AFTER_LAST_EPISODE_REWATCHING=yes
ADD_ENTRY_IF_MISSING=no
CACHE_REFRESH_RATE=24
CACHE_MODE=NORMAL
SILENT_MODE=no
]]

-- Try script-opts directory (sibling to scripts)
local script_opts_dir = script_dir:match("^(.-)[/\\]scripts[/\\]")

if script_opts_dir then
    script_opts_dir = utils.join_path(script_opts_dir, "script-opts")
else
    -- Fallback: try to find mpv config dir
    local mpv_conf_dir = get_mpv_config_dir()
    script_opts_dir = mpv_conf_dir and utils.join_path(mpv_conf_dir, "script-opts") or nil
end

local script_opts_path = script_opts_dir and utils.join_path(script_opts_dir, conf_name) or nil

-- Try script directory
local script_path = utils.join_path(script_dir, conf_name)

-- Try mpv config directory
local mpv_conf_dir = get_mpv_config_dir()
local mpv_conf_path = mpv_conf_dir and utils.join_path(mpv_conf_dir, conf_name) or nil

local conf_paths = {script_opts_path, script_path, mpv_conf_path}

-- Try to find config file
local conf_path = nil
for _, path in ipairs(conf_paths) do
    if path then
        local f = io.open(path, "r")
        if f then
            f:close()
            conf_path = path
            break
        end
    end
end

-- If not found, try to create in order
if not conf_path then
    for _, path in ipairs(conf_paths) do
        if path then
            local f = io.open(path, "w")
            if f then
                f:write(default_conf)
                f:close()
                conf_path = path
                break
            end
        end
    end
end

-- If still not found or created, warn and use defaults
if not conf_path then
    mp.msg.warn("Could not find or create anilistUpdater.conf in any known location! Using default options.")
end

-- Initialize options with defaults
local options = {
    DIRECTORIES = "",
    EXCLUDED_DIRECTORIES = "",
    UPDATE_PERCENTAGE = 85,
    SET_COMPLETED_TO_REWATCHING_ON_FIRST_EPISODE = false,
    UPDATE_PROGRESS_WHEN_REWATCHING = true,
    SET_TO_COMPLETED_AFTER_LAST_EPISODE_CURRENT = true,
    SET_TO_COMPLETED_AFTER_LAST_EPISODE_REWATCHING = true,
    ADD_ENTRY_IF_MISSING = false,
    CACHE_REFRESH_RATE = 24,
    CACHE_MODE = "NORMAL",
    SILENT_MODE = false
}

-- Override defaults with values from config file
mpoptions.read_options(options, "anilistUpdater")

-- Parse DIRECTORIES and EXCLUDED_DIRECTORIES using helper function
options.DIRECTORIES = parse_directory_string(options.DIRECTORIES)
options.EXCLUDED_DIRECTORIES = parse_directory_string(options.EXCLUDED_DIRECTORIES)

-- When calling Python, pass only the options relevant to it
local python_options = {
    SET_COMPLETED_TO_REWATCHING_ON_FIRST_EPISODE = options.SET_COMPLETED_TO_REWATCHING_ON_FIRST_EPISODE,
    UPDATE_PROGRESS_WHEN_REWATCHING = options.UPDATE_PROGRESS_WHEN_REWATCHING,
    SET_TO_COMPLETED_AFTER_LAST_EPISODE_CURRENT = options.SET_TO_COMPLETED_AFTER_LAST_EPISODE_CURRENT,
    SET_TO_COMPLETED_AFTER_LAST_EPISODE_REWATCHING = options.SET_TO_COMPLETED_AFTER_LAST_EPISODE_REWATCHING,
    ADD_ENTRY_IF_MISSING = options.ADD_ENTRY_IF_MISSING,
    CACHE_REFRESH_RATE = tonumber(options.CACHE_REFRESH_RATE) or 24,
    CACHE_MODE = tostring(options.CACHE_MODE or "NORMAL")
}
local python_options_json = utils.format_json(python_options)

DIRECTORIES = options.DIRECTORIES
EXCLUDED_DIRECTORIES = options.EXCLUDED_DIRECTORIES
UPDATE_PERCENTAGE = tonumber(options.UPDATE_PERCENTAGE) or 85

local current_anime_info = nil
local get_path

local function path_starts_with_any(path, directories)
    local norm_path = normalize_path(path)
    for _, dir in ipairs(directories) do
        if norm_path:sub(1, #dir) == dir then
            return true
        end
    end
    return false
end

local function parse_detected_info(result)
    if not result or not result.stdout then
        return nil
    end

    for line in result.stdout:gmatch("[^\r\n]+") do
        local json_part = line:match("^INFO:%s*(.+)$")
        if json_part then
            local info = utils.parse_json(json_part)
            if info and type(info) == "table" then
                return info
            end
        end
    end

    return nil
end

function callback(success, result, error)
    local is_success = success and result and result.status == 0

    -- removed eager local progress update

    -- Don't show any messages only if the result is successful
    if options.SILENT_MODE and is_success then return end
    
    local messages = {}
    local prompt_rewatch_name = nil
    local prompt_score_name = nil
    local score_format = nil
    local current_score = nil

    if result and result.stdout then
        for line in result.stdout:gmatch("[^\r\n]+") do
            local msg = line:match("^OSD:%s*(.-)%s*$")
            local rw_name = line:match("^PROMPT_REWATCH:%s*(.-)%s*$")
            local s_name, s_format, s_curr = line:match("^PROMPT_SCORE:(.-):(.-):(.-)$")
            if s_name then
                prompt_score_name = s_name
                score_format = s_format
                current_score = s_curr
            elseif rw_name then
                prompt_rewatch_name = rw_name
            elseif msg then
                table.insert(messages, msg)
            else
                print(line)
            end
        end
    end
    
    if prompt_rewatch_name then
        mp.osd_message('Rewatch "' .. prompt_rewatch_name .. '"? (ENTER: yes, ESC: no)', 10)
        local function accept_rewatch()
            mp.osd_message("Setting to REPEATING...", 3)
            mp.remove_key_binding("accept_rewatch")
            mp.remove_key_binding("cancel_rewatch")
            local path = get_path()
            local info_json = utils.format_json(current_anime_info)
            mp.command_native_async({
                name = "subprocess",
                args = {python_command, script_dir .. "anilistUpdater.py", path, "set_rewatching", python_options_json, info_json},
                capture_stdout = true
            }, callback)
        end
        local function cancel_rewatch()
            mp.osd_message("Cancelled rewatch.", 3)
            mp.remove_key_binding("accept_rewatch")
            mp.remove_key_binding("cancel_rewatch")
        end
        mp.add_forced_key_binding("ENTER", "accept_rewatch", accept_rewatch)
        mp.add_forced_key_binding("ESC", "cancel_rewatch", cancel_rewatch)
        return
    end

    if prompt_score_name then
        local input_score = ""
        local prompt_timer = nil

        local function render_prompt()
            local has_score = current_score and current_score ~= "None" and current_score ~= ""
            local curr = has_score and (" (Current: " .. current_score .. ")") or ""
            local esc_msg = has_score and "(ESC to not change)" or "(ESC to skip)"
            local format_desc = score_format
            if score_format == "POINT_100" then format_desc = "1-100"
            elseif score_format == "POINT_10_DECIMAL" then format_desc = "1.0-10.0"
            elseif score_format == "POINT_10" then format_desc = "1-10"
            elseif score_format == "POINT_5" then format_desc = "1-5"
            elseif score_format == "POINT_3" then format_desc = "1-3" end
            
            local msg = 'Finished "' .. prompt_score_name .. '"' .. curr .. '\nRate (' .. format_desc .. '): ' .. input_score .. '_\nENTER to submit, ' .. esc_msg
            mp.osd_message(msg, 1)
        end

        prompt_timer = mp.add_periodic_timer(0.25, render_prompt)

        local function cleanup_bindings()
            if prompt_timer then
                prompt_timer:kill()
                prompt_timer = nil
            end
            mp.osd_message("", 0)
            mp.remove_key_binding("score_enter")
            mp.remove_key_binding("score_esc")
            mp.remove_key_binding("score_bs")
            for i = 0, 9 do
                mp.remove_key_binding("score_" .. i)
                mp.remove_key_binding("score_kp" .. i)
            end
            mp.remove_key_binding("score_dot")
            mp.remove_key_binding("score_kp_dot")
            mp.remove_key_binding("score_comma")
        end

        local function submit_score()
            cleanup_bindings()
            local safe_path = get_path() or ""
            local safe_info = current_anime_info and utils.format_json(current_anime_info) or "{}"
            local safe_opts = python_options_json or "{}"
            local safe_cmd = python_command or "python"
            local sanitized_score = input_score:gsub(",", ".")

            if sanitized_score == "" then
                mp.osd_message("Setting to COMPLETED without score...", 3)
                mp.command_native_async({
                    name = "subprocess",
                    args = {safe_cmd, script_dir .. "anilistUpdater.py", safe_path, "set_completed_no_score", safe_opts, safe_info},
                    capture_stdout = true
                }, callback)
            else
                mp.osd_message("Setting to COMPLETED with score " .. sanitized_score .. "...", 3)
                mp.command_native_async({
                    name = "subprocess",
                    args = {safe_cmd, script_dir .. "anilistUpdater.py", safe_path, "set_completed_with_score", safe_opts, safe_info, sanitized_score},
                    capture_stdout = true
                }, callback)
            end
        end

        local function cancel_score()
            cleanup_bindings()
            local has_score = current_score and current_score ~= "None" and current_score ~= ""
            if has_score then
                mp.osd_message("Kept current score. Setting to COMPLETED...", 3)
            else
                mp.osd_message("Skipped rating. Setting to COMPLETED...", 3)
            end
            local safe_path = get_path() or ""
            local safe_info = current_anime_info and utils.format_json(current_anime_info) or "{}"
            local safe_opts = python_options_json or "{}"
            local safe_cmd = python_command or "python"
            
            mp.command_native_async({
                name = "subprocess",
                args = {safe_cmd, script_dir .. "anilistUpdater.py", safe_path, "set_completed_no_score", safe_opts, safe_info},
                capture_stdout = true
            }, callback)
        end

        local function add_char(c)
            input_score = input_score .. c
            render_prompt()
        end

        local function backspace()
            if #input_score > 0 then
                input_score = input_score:sub(1, -2)
                render_prompt()
            end
        end

        mp.add_forced_key_binding("ENTER", "score_enter", submit_score)
        mp.add_forced_key_binding("ESC", "score_esc", cancel_score)
        mp.add_forced_key_binding("BS", "score_bs", backspace)
        for i = 0, 9 do
            mp.add_forced_key_binding(tostring(i), "score_" .. i, function() add_char(tostring(i)) end)
            mp.add_forced_key_binding("KP" .. tostring(i), "score_kp" .. i, function() add_char(tostring(i)) end)
        end
        mp.add_forced_key_binding(".", "score_dot", function() add_char(".") end)
        mp.add_forced_key_binding("KP_DEC", "score_kp_dot", function() add_char(".") end)
        mp.add_forced_key_binding(",", "score_comma", function() add_char(",") end)

        render_prompt()
        return
    end

    if is_success then
        if #messages == 0 then
            table.insert(messages, "Updated anime correctly.")
        end
    end

    if #messages > 0 then
        mp.osd_message(table.concat(messages, "\n"), 5)
    end
end

local function get_python_command()
    local platform = mp.get_property("platform")
    if platform == "windows" then
        return "python"
    else
        return "python3"
    end
end

-- Helper to open a path or URL with the system's default handler
local function open(target)
    local platform = mp.get_property("platform")
    local args
    if platform == "windows" then
        args = {"cmd", "/c", "start", "", target}
    elseif platform == "darwin" then
        args = {"open", target}
    else
        args = {"xdg-open", target}
    end
    mp.command_native({name = "subprocess", args = args, detach = true})
end

-- Helper function to detect ani-cli compatibility
local function is_ani_cli_compatible()
    local directory = mp.get_property("working-directory") or ""
    local file_path = mp.get_property("path") or ""
    local full_path = utils.join_path(directory, file_path)
    
    -- Auto-detect ani-cli compatibility by checking for http:// or https:// anywhere in the path
    return full_path:match("https?://") ~= nil
end

get_path = function()
    local directory = mp.get_property("working-directory")
    -- It seems like in Linux working-directory sometimes returns it without a "/" at the end
    directory = (directory:sub(-1) == '/' or directory:sub(-1) == '\\') and directory or directory .. '/'
    -- For some reason, "path" sometimes returns the absolute path, sometimes it doesn't.
    local file_path = mp.get_property("path")
    local path = utils.join_path(directory, file_path)

    -- Auto-detect ani-cli compatibility by checking for http:// or https:// anywhere in the path
    if path:match("https?://") then
        local media_title = mp.get_property("media-title")
        if media_title and media_title ~= "" then
            return media_title
        end
    end

    if path:match("([^/\\]+)$"):lower() == "file.mp4" then
        path = mp.get_property("media-title")
    end

    return path
end

local python_command = get_python_command()

local isPaused = false
local is_file_eligible = false
local is_fetching = false

local function fetch_anime_info(cb)
    if is_fetching then
        return
    end
    is_fetching = true

    local path = get_path()
    mp.command_native_async({
        name = "subprocess",
        args = {python_command, script_dir .. "anilistUpdater.py", path, "info", python_options_json},
        capture_stdout = true
    }, function(success, result)
        is_fetching = false
        if success and result and result.status == 0 then
            current_anime_info = parse_detected_info(result)
            if current_anime_info then
                print("Detected anime: " .. (current_anime_info.anime_name or "?") .. " #" .. (current_anime_info.episode or "?"))
            end
        end
        if cb then
            cb(current_anime_info)
        end
    end)
end

-- Make sure it doesnt trigger twice in 1 video
local triggered = false
-- Check progress every X seconds (when not paused)
local UPDATE_INTERVAL = 1

-- Initialize timer once - we control it with stop/resume
local progress_timer = mp.add_periodic_timer(UPDATE_INTERVAL, function()
    if triggered then
        return
    end
    
    local percent_pos = mp.get_property_number("percent-pos")
    if not percent_pos then
        return
    end

    if percent_pos >= UPDATE_PERCENTAGE then
        update_anilist()
        triggered = true
        if progress_timer then
            progress_timer:stop()
        end
        return
    end
end)
-- Start with timer stopped - it will be started when a valid file loads
progress_timer:stop()

-- Handle pause/unpause events to control the timer
function on_pause_change(name, value)
    isPaused = value
    if value then
        progress_timer:stop()
    else
        if is_file_eligible and not triggered then
            progress_timer:resume()
        end
    end
end

local function update(info)
    local path = get_path()
    local info_json = utils.format_json(info)

    mp.command_native_async({
        name = "subprocess",
        args = {python_command, script_dir .. "anilistUpdater.py", path, "update_with_info", python_options_json, info_json},
        capture_stdout = true
    }, callback)
end

-- Function to launch the .py script to update AniList
function update_anilist()
    if current_anime_info then
        update(current_anime_info)
    else
        fetch_anime_info(function(info)
            if info then
                update(info)
            else
                if not options.SILENT_MODE then
                    mp.osd_message("Error: Anime info not loaded yet.", 3)
                end
            end
        end)
    end
end

mp.observe_property("pause", "bool", on_pause_change)

-- Reset triggered and start/stop timer based on file loading
mp.register_event("file-loaded", function()
    triggered = false
    is_file_eligible = false
    current_anime_info = nil
    is_fetching = false
    progress_timer:stop()

    if not is_ani_cli_compatible() and #DIRECTORIES > 0 then
        local path = get_path()

        if not path_starts_with_any(path, DIRECTORIES) then
            return
        else
            -- If it starts with the directories, check if it starts with any of the excluded directories
            if #EXCLUDED_DIRECTORIES > 0 and path_starts_with_any(path, EXCLUDED_DIRECTORIES) then
                return
            end
        end
    end

    is_file_eligible = true

    -- Fetch anime info on file load
    fetch_anime_info()

    -- Start timer for this file
    if not isPaused then
        progress_timer:resume()
    end
end)

-- Default keybinds - can be customized in input.conf using script-binding commands
mp.add_key_binding("ctrl+a", 'update_anilist', update_anilist)

local function launch(info)
    local url = "https://anilist.co/anime/" .. info.anime_id
    if not options.SILENT_MODE then
        mp.osd_message('Opening AniList for "' .. (info.anime_name or "?") .. '"', 3)
    end
    open(url)
end

local function launch_anilist()
    if current_anime_info and current_anime_info.anime_id then
        launch(current_anime_info)
    else
        fetch_anime_info(function(info)
            if info and info.anime_id then
                launch(info)
            else
                if not options.SILENT_MODE then
                    mp.osd_message("Error: Anime info not loaded yet.", 3)
                end
            end
        end)
    end
end

mp.add_key_binding("ctrl+b", 'launch_anilist', launch_anilist)

-- Open the folder that the video is
function open_folder()
    local path = mp.get_property("path")
    local directory

    if not path then
        mp.msg.warn("No file is currently playing.")
        return
    end

    directory = path:match("(.+)[/\\]") or mp.get_property("working-directory")

    open(directory)
end

mp.add_key_binding("ctrl+d", 'open_folder', open_folder)

-- Initialize and bind correction overlay module
correction_overlay.init({
    python_command = python_command,
    python_options_json = python_options_json,
    callback = callback,
    get_current_anime_info = function() return current_anime_info end,
    set_current_anime_info = function(info) current_anime_info = info end
})

mp.add_key_binding("c", 'correct_anime_id', function()
    correction_overlay.correct_anime_id(get_path)
end)
