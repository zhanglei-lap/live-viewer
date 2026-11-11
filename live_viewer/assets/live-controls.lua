-- Live-only supplement to mpv's bundled OSC. No URLs, accounts or network I/O.
local mp = require 'mp'
local assdraw = require 'mp.assdraw'
local overlay = mp.create_osd_overlay('ass-events')
local status, shown_until = '准备播放…', 0
local bounds, dragging, last_seek = nil, false, 0

local function live()
    mp.commandv('script-message', 'viewer-return-live')
end

local function text_escape(value)
    return tostring(value or ''):gsub('\\', '\\e'):gsub('{', '\\{'):gsub('\n', ' ')
end

local function range()
    local cache = mp.get_property_native('demuxer-cache-state', {})
    local position = mp.get_property_number('time-pos')
    if not position then return nil end
    local best
    for _, r in ipairs(cache['seekable-ranges'] or {}) do
        -- The latest seekable keyframe can lag the currently displayed frame.
        if r.start and r['end'] and r['end'] > r.start and position >= r.start - 0.1 then
            if not best or r['end'] > best['end'] then best = r end
        end
    end
    if best then return best.start, best['end'], position end
end

local function seek_at(x)
    local a, b = range()
    if not bounds or not a or b - a < 1 then return end
    local ratio = math.max(0, math.min(1, (x - bounds.x) / bounds.width))
    -- Stay inside the real seekable range, never request a CDN historical seek.
    local target = math.min(b - 0.25, math.max(a + 0.05, a + ratio * (b - a)))
    mp.commandv('seek', tostring(target), 'absolute+exact')
    last_seek = mp.get_time()
end

local function click()
    if not bounds then return end
    local x, y = mp.get_mouse_pos()
    if y < bounds.y0 or y > bounds.y1 then return end
    if x >= bounds.live_x then
        live()
    elseif x >= bounds.x and x <= bounds.x + bounds.width then
        dragging = true
        seek_at(x)
    end
end

mp.set_key_bindings({{'MBTN_LEFT', function() click(); dragging = false end, click,
                     function() dragging = false end}}, 'viewer-cache', 'force')

local function draw()
    local w, h = mp.get_osd_size()
    if not w or w < 320 or h < 200 then return end
    local mouse_x, mouse_y = mp.get_mouse_pos()
    local scale = h / 720
    -- Use mpv's slim controls below, with one live-only cache bar above them.
    local y = h - 104 * scale
    local visible = dragging or mp.get_time() < shown_until or mouse_y > y - 30 * scale or mp.get_property_native('pause')
    if not visible then
        bounds = nil
        overlay:remove()
        mp.disable_key_bindings('viewer-cache')
        return
    end
    local a, b, position = range()
    local margin, button = 24 * scale, 126 * scale
    local width = w - margin * 2 - button
    bounds = {x = margin, width = width, live_x = w - margin - button + 18 * scale,
              y0 = y - 28 * scale, y1 = y + 26 * scale}
    local ass = assdraw.ass_new()
    ass:new_event()
    ass:pos(0, 0)
    ass:append('{\\an7\\bord0\\shad0\\1c&H131313&\\1a&H38&}')
    ass:draw_start(); ass:rect_cw(0, y - 28 * scale, w, y + 26 * scale); ass:draw_stop()
    ass:new_event(); ass:pos(margin, y - 25 * scale)
    ass:append('{\\an7\\bord0\\shad0\\fs' .. 17 * scale .. '\\1c&HE0E0E0&}')
    local label = '缓存准备中'
    if a then
        label = string.format('本次缓存 %.0f 秒 · 距缓存前沿 %.1f 秒', b - a, math.max(0, b - position))
    end
    local current = mp.get_property_number('time-pos', 0)
    local volume = mp.get_property_number('volume', 0)
    local audio = mp.get_property_native('mute') and '静音' or string.format('音量 %.0f%%', volume)
    label = string.format('%02d:%02d:%02d · ', math.floor(current / 3600), math.floor(current / 60) % 60, math.floor(current) % 60) .. label .. ' · ' .. audio
    if mp.get_property_native('pause') then label = '已暂停 · 继续播放将回到直播' end
    ass:append(text_escape(label))
    ass:new_event(); ass:pos(0, 0); ass:append('{\\an7\\bord0\\shad0\\1c&H555555&}')
    ass:draw_start(); ass:rect_cw(margin, y + 6 * scale, margin + width, y + 10 * scale); ass:draw_stop()
    if a then
        local ratio = math.max(0, math.min(1, (position - a) / (b - a)))
        ass:new_event(); ass:pos(0, 0); ass:append('{\\an7\\bord0\\shad0\\1c&HDFC549&}')
        ass:draw_start(); ass:rect_cw(margin, y + 6 * scale, margin + width * ratio, y + 10 * scale)
        ass:rect_cw(margin + width * ratio - 3 * scale, y + 1 * scale, margin + width * ratio + 3 * scale, y + 15 * scale)
        ass:draw_stop()
    end
    ass:new_event(); ass:pos(bounds.live_x, y - 8 * scale)
    ass:append('{\\an7\\bord0\\shad0\\fs' .. 20 * scale .. '\\1c&HDFC549&}回到直播')
    if mp.get_property_native('idle-active') then
        ass:new_event(); ass:pos(w / 2, h / 2)
        ass:append('{\\an5\\bord0\\shad0\\fs' .. 22 * scale .. '\\1c&HE0E0E0&}' .. text_escape(status))
    end
    overlay.res_x, overlay.res_y, overlay.data = w, h, ass.text
    overlay:update()
    mp.set_mouse_area(0, y - 28 * scale, w, y + 26 * scale, 'viewer-cache')
    mp.enable_key_bindings('viewer-cache', 'allow-hide-cursor')
    if dragging and mp.get_time() - last_seek >= 0.15 then seek_at(mouse_x) end
end

mp.observe_property('mouse-pos', 'native', function()
    shown_until = mp.get_time() + 2
end)
mp.register_event('file-loaded', function() shown_until = mp.get_time() + 3 end)
mp.register_script_message('viewer-status', function(value) status = value end)
mp.register_script_message('viewer-help', function()
    mp.osd_message('空格 暂停 / 继续　F 全屏　Esc 退出全屏\n左右键 在缓存内回看　L 回到直播　上下键 音量', 4)
end)
mp.add_forced_key_binding('l', 'return-live', live)
mp.add_forced_key_binding('ESC', 'leave-fullscreen', function() mp.set_property_native('fullscreen', false) end)
mp.add_forced_key_binding('UP', 'volume-up', function() mp.commandv('add', 'volume', '5') end)
mp.add_forced_key_binding('DOWN', 'volume-down', function() mp.commandv('add', 'volume', '-5') end)
-- Default seek shortcuts must not jump beyond a continuous live stream's cache.
mp.add_forced_key_binding('LEFT', 'cache-back', function()
    local a, b, pos = range()
    if a then mp.commandv('seek', tostring(math.max(a + 0.05, pos - 5)), 'absolute+exact') end
end)
mp.add_forced_key_binding('RIGHT', 'cache-forward', function()
    local a, b, pos = range()
    if a then mp.commandv('seek', tostring(math.min(b - 0.25, pos + 5)), 'absolute+exact') end
end)
mp.add_periodic_timer(0.12, draw)
