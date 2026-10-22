

def test_trim_finds_small_icon_inside_windows_thumbnail_plate():
    from PIL import Image, ImageDraw
    from pandadeck.music.images import trim
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((4, 4, 251, 251), outline=(200, 200, 200, 70), width=3)   # faint plate + shadow ring
    d.rectangle((104, 104, 151, 151), fill=(20, 180, 60, 255))           # the real 48 px icon
    out = trim(img)
    assert 48 <= out.width <= 56 and out.width == out.height
    full = Image.new("RGBA", (256, 256), (10, 120, 220, 255))               # a normal full-size icon
    assert trim(full).width >= 250


def test_square_user_pictures_get_rounded_corners_but_shaped_ones_dont():
    from PIL import Image
    from pandadeck.music.images import rounded_if_square
    sq = rounded_if_square(Image.new("RGBA", (200, 200), (20, 160, 60, 255)))
    assert sq.getchannel("A").getpixel((0, 0)) == 0 and sq.getchannel("A").getpixel((100, 100)) == 255
    round_logo = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    round_logo.paste((200, 0, 0, 255), (40, 40, 160, 160))
    assert rounded_if_square(round_logo).tobytes() == round_logo.tobytes()


def test_backdrops_of_white_pictures_are_dark_enough_for_white_text():
    from PIL import Image
    from pandadeck.music import images as im
    white = Image.new("RGB", (600, 600), (245, 245, 240))
    for bg in (im.backdrop_image(white), im.photo_backdrop_image(white)):
        mean = sum(bg.convert("L").tobytes()) / (bg.width * bg.height * 255)
        assert mean <= 0.22
