import hashlib
import urllib.parse
import random
import sqlite3

from .common import CommonTranslator, InvalidServerResponse, MissingAPIKeyException


class OCRTextTranslator(CommonTranslator):
    _LANGUAGE_CODE_MAP = {
        'CHS': 'zh',
        'CHT': 'cht',
        'JPN': 'ja',
        'ENG': 'en',
        'KOR': 'kor',
        'VIN': 'vie',
        'CSY': 'cs',
        'NLD': 'nl',
        'FRA': 'fra',
        'DEU': 'de',
        'HUN': 'hu',
        'ITA': 'it',
        'PLK': 'pl',
        'PTB': 'pt',
        'ROM': 'rom',
        'RUS': 'ru',
        'ESP': 'spa',
    }
    # New class attribute for counting calls
    _translate_call_count = 0
    def __init__(self) -> None:
        super().__init__()

    async def _translate(self, from_lang, to_lang, queries):
        # Increment the call counter
        OCRTextTranslator._translate_call_count += 1
        print(OCRTextTranslator._translate_call_count)
        # Proceed with the method's existing logic up to the database interaction part

        # Check if the call count is odd before writing to the database
        if OCRTextTranslator._translate_call_count % 2 != 0:
            print(OCRTextTranslator._translate_call_count)
            # Create a connection and open the database
            conn = sqlite3.connect('manga_page.db')

            # Create a cursor object
            cursor = conn.cursor()
            # Check page_count table exist or none
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='page_count'")
            if not cursor.fetchone():
                # page_count table not exist
                cursor.execute("CREATE TABLE page_count (page INTEGER)")
                cursor.execute("INSERT INTO page_count (page) VALUES (1)")
                conn.commit()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='manga_page'")
            # Check manga_page table exist or none
            if not cursor.fetchone():
                # manga_page table not exist
                cursor.execute("CREATE TABLE manga_page (id INTEGER PRIMARY KEY AUTOINCREMENT, words TEXT, trans TEXT)")
                conn.commit()
            # Read the page field value of the first piece of data in the page_count table
            cursor.execute('SELECT page FROM page_count LIMIT 1')
            result = cursor.fetchone()
            print("this is result")
            print(result[0])
            page = result[0]
            print("this is page")
            print(page)
            atext = ""
            atext = "@Page " + str(page) + "，" + str(
                len(queries)) + " sentences in total.\r\n"  # Test to add special symbols
            # atext = "Page "+str(page)+", a total of "+str(len(queries))+" sentences.\r\n\r\n" # Test to add special symbols

            result_list = []  # by number
            atext = f"@Page {page}, {len(queries)} sentences in total.\n"
            for i, text in enumerate(queries):
                atext += f"{i + 1}.{text}\r\n"
            # result_list.append(str(i+1)) # tagging order
            atext = atext + "@Page "+str(page)+" End"
            # filter[]
            atext = atext.replace("[", "")
            atext = atext.replace("]", "")

            print("Overview of the current text：")
            print(atext)
            print("import database")

            # Update page field value
            new_page = page + 1
            cursor.execute('UPDATE page_count SET page=? WHERE rowid=1', (new_page,))

            # create form
            # cursor.execute('''CREATE TABLE manga_page(id INTEGER PRIMARY KEY AUTOINCREMENT, words TEXT, trans TEXT)''')

            # insert data
            # data = 'Hello, World!'
            cursor.execute("INSERT INTO manga_page (words) VALUES (?)", (atext,))

            # commit changes
            conn.commit()

            # close connection
            conn.close()
            return result_list
        result_list = []
        return result_list