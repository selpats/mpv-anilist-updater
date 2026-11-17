local msg = require("mp.msg")
local config = require("lua.config")
local backend = require("lua.backend")
local overlay = require("lua.overlay")

local silent = config.options.SILENT_MODE

local state = {
    info = nil, -- detected anime for the current file
    generation = 0, -- bumped per file, so late responses for a previous file are dropped
    waiting = {}, -- callbacks for the detection in flight
    fetching = false,
}

local progress_timer

local function osd(text, seconds)
    if not silent then
        mp.osd_message(text, seconds or 3)
    end
end

local function warning_text(response)
    if response and type(response.warnings) == "table" and #response.warnings > 0 then
        return table.concat(response.warnings, "\n")
    end
    return nil
end

-- Show a backend response. Failures to find or update something are only worth interrupting for
-- when the user asked; real errors and secondary tracker auth warnings always are.
local function notify(response, manual)
    local kind = response.kind
    local warnings = warning_text(response)
    if warnings then
        msg.warn(warnings)
    end
    if kind == "cancelled" then
        return
    elseif kind == "error" then
        local text = warnings and (response.message .. "\n" .. warnings) or response.message
        mp.osd_message(text, warnings and 6 or 5)
    elseif kind == "skip" then
        if manual and not silent and response.message then
            local text = warnings and (response.message .. "\n" .. warnings) or response.message
            mp.osd_message(text, warnings and 6 or 5)
        elseif warnings then
            mp.osd_message(warnings, 5)
        end
    elseif response.ok then
        local body = (not silent) and (response.message or "Updated anime correctly.") or nil
        if body and warnings then
            mp.osd_message(body .. "\n" .. warnings, 6)
        elseif warnings then
            mp.osd_message(warnings, 5)
        elseif body then
            mp.osd_message(body, 5)
        end
    end
end

-- Detect the anime once; everyone who asks meanwhile shares the same request.
-- callback(info, response) gets a nil info when detection failed.
local function ensure_info(callback)
    if state.info then
        return callback and callback(state.info)
    end
    table.insert(state.waiting, callback or function() end)
    if state.fetching then
        return
    end

    state.fetching = true
    local generation = state.generation
    backend.call("info", {}, function(response)
        if generation ~= state.generation then
            return
        end
        state.fetching = false
        state.info = response.ok and response.info or nil
        local warnings = warning_text(response)
        if warnings then
            msg.warn(warnings)
        end
        if state.info then
            local detected = (state.info.anime_name or "?") .. " #" .. (state.info.episode or "?")
            msg.info("Detected anime: " .. detected)
            if config.options.SHOW_DETECTED_ANIME and not silent then
                local text = "Detected: " .. detected
                mp.osd_message(warnings and (text .. "\n" .. warnings) or text, warnings and 6 or 3)
            elseif warnings then
                mp.osd_message(warnings, 5)
            end
        elseif warnings then
            mp.osd_message(warnings, 5)
        end

        local waiting = state.waiting
        state.waiting = {}
        for _, waiter in ipairs(waiting) do
            waiter(state.info, response)
        end
    end)
end

local function update_anilist(manual)
    if manual then
        osd("Updating AniList...", 2)
    end

    ensure_info(function(info, response)
        if not info then
            if response and response.kind ~= "cancelled" and response.message then
                notify(response, manual)
            elseif manual then
                mp.osd_message("Error: Anime info not loaded yet.", 3)
            end
            return
        end

        local generation = state.generation
        backend.call("update", { info = info }, function(result)
            if result.ok and result.info and generation == state.generation then
                state.info = result.info
                progress_timer:kill()
            end
            notify(result, manual)
        end)
    end)
end

local function open(target, folder)
    local platform = mp.get_property("platform")
    local args = { "xdg-open", target }
    if platform == "windows" then
        -- start would read & and ^ in a folder name as cmd syntax
        args = folder and { "explorer", target } or { "cmd", "/c", "start", "", target }
    elseif platform == "darwin" then
        args = { "open", target }
    end
    mp.command_native({ name = "subprocess", args = args, detach = true })
end

local function launch_anilist()
    ensure_info(function(info, response)
        if info and info.anime_id then
            osd('Opening AniList for "' .. (info.anime_name or "?") .. '"')
            open("https://anilist.co/anime/" .. info.anime_id)
        elseif response and response.kind ~= "cancelled" and response.message then
            notify(response, true)
        else
            mp.osd_message("Error: Anime info not loaded yet.", 3)
        end
    end)
end

local function open_folder()
    local path = mp.get_property("path")
    if not path then
        return msg.warn("No file is currently playing.")
    elseif backend.is_stream() then
        return mp.osd_message("Not a local file.", 2)
    end
    open(path:match("(.+)[/\\]") or mp.get_property("working-directory"), true)
end

-- Update once the video is far enough along. percent-pos doesn't move while paused.
progress_timer = mp.add_periodic_timer(1, function()
    local percent = mp.get_property_number("percent-pos")
    if percent and percent >= config.options.UPDATE_PERCENTAGE then
        progress_timer:kill()
        update_anilist(false)
    end
end)
progress_timer:kill()

mp.register_event("file-loaded", function()
    progress_timer:kill()
    state.generation = state.generation + 1
    state.info, state.waiting, state.fetching = nil, {}, false

    if not backend.is_ani_cli() then
        local path = backend.get_path()
        if not path or backend.is_stream() or not config.is_eligible(path) then
            return
        end
    end

    -- Detect early so the update and the keybinds have the info ready
    ensure_info()
    progress_timer:resume()
end)

mp.register_event("end-file", function()
    progress_timer:kill()
end)

mp.add_key_binding("ctrl+a", "update_anilist", function() update_anilist(true) end)
mp.add_key_binding("ctrl+b", "launch_anilist", launch_anilist)
mp.add_key_binding("ctrl+d", "open_folder", open_folder)

overlay.init({
    backend = backend,
    notify = notify,
    ensure_info = ensure_info,
    generation = function() return state.generation end,
    set_info = function(info) state.info = info end,
})
