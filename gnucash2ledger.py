#! /usr/bin/python3

""" gnucash2ledger.py

This script creates a text file formmatted to work with the Ledger and hledger
command-line tools from a Gnucash file. 

The Gnucash MUST be saved as an uncompressed XML file from within Gnuchash. This
script will parse the XML file and will not modify the original Gnucash file.

"""

import os
import sys
import argparse
import dateutil.parser
import xml.etree.ElementTree
import locale
from datetime import date
from currency_symbols import CurrencySymbols
from tqdm import tqdm

locale.setlocale(locale.LC_ALL, "")

nss = {
    "gnc": "http://www.gnucash.org/XML/gnc",
    "act": "http://www.gnucash.org/XML/act",
    "book": "http://www.gnucash.org/XML/book",
    "cd": "http://www.gnucash.org/XML/cd",
    "cmdty": "http://www.gnucash.org/XML/cmdty",
    "price": "http://www.gnucash.org/XML/price",
    "slot": "http://www.gnucash.org/XML/slot",
    "split": "http://www.gnucash.org/XML/split",
    "sx": "http://www.gnucash.org/XML/sx",
    "trn": "http://www.gnucash.org/XML/trn",
    "ts": "http://www.gnucash.org/XML/ts",
    "fs": "http://www.gnucash.org/XML/fs",
    "bgt": "http://www.gnucash.org/XML/bgt",
    "recurrence": "http://www.gnucash.org/XML/recurrence",
    "lot": "http://www.gnucash.org/XML/lot",
    "addr": "http://www.gnucash.org/XML/addr",
    "owner": "http://www.gnucash.org/XML/owner",
    "billterm": "http://www.gnucash.org/XML/billterm",
    "bt-days": "http://www.gnucash.org/XML/bt-days",
    "bt-prox": "http://www.gnucash.org/XML/bt-prox",
    "cust": "http://www.gnucash.org/XML/cust",
    "employee": "http://www.gnucash.org/XML/employee",
    "entry": "http://www.gnucash.org/XML/entry",
    "invoice": "http://www.gnucash.org/XML/invoice",
    "job": "http://www.gnucash.org/XML/job",
    "order": "http://www.gnucash.org/XML/order",
    "taxtable": "http://www.gnucash.org/XML/taxtable",
    "tte": "http://www.gnucash.org/XML/tte",
    "vendor": "http://www.gnucash.org/XML/vendor",
}


class DefaultAttributeProducer:
    def __init__(self, defaultValue):
        self.__defaultValue = defaultValue
        
    def __getattr__(self, value):
         return self.__defaultValue


def orElse(var, default=""):
    if var is None:
        return DefaultAttributeProducer(default)
    else:
        return var


class Commodity:
    def __init__(self, e, useSymbols=False):
        """From a XML e representing a commodity, generates a representation of
        the commodity
        """

        self.space = orElse(e.find("cmdty:space", nss)).text
        if useSymbols:
            self.id = getCurrencySymbol(orElse(e.find("cmdty:id", nss)).text)
        else:
            self.id = orElse(e.find("cmdty:id", nss)).text
        self.name = orElse(e.find("cmdty:name", nss)).text

    def toLedgerFormat(self):
        """Format the commodity in a way good to be interpreted by ledger."""
        outPattern = "commodity {id}\n" "    note {name} ({space}:{id})\n"

        return outPattern.format(**self.__dict__)


class Account:
    def __init__(self, accountDb, e, useSymbols=False):
        self.accountDb = accountDb
        self.name = e.find("act:name", nss).text
        self.id = e.find("act:id", nss).text
        self.accountDb[self.id] = self
        self.description = orElse(e.find("act:description", nss)).text
        self.type = e.find("act:type", nss).text
        self.parent = orElse(e.find("act:parent", nss), None).text
        self.used = False  # Mark accounts that were in a transaction
        if useSymbols:
            self.commodity = getCurrencySymbol(orElse(e.find("act:commodity/cmdty:id", nss), None).text)
        else:
            self.commodity = orElse(e.find("act:commodity/cmdty:id", nss), None).text

    def getParent(self):
        return self.accountDb[self.parent]

    def fullName(self):
        if self.parent is not None and self.getParent().type != "ROOT":
            prefix = self.getParent().fullName() + ":"
        else:
            prefix = ""  # ROOT will not be displayed
        return prefix + self.name

    def toLedgerFormat(self):
        outPattern = (
            "account {fullName}\n"
            "    note {description} (type: {type})\n"
        )
        return outPattern.format(
            fullName=self.fullName(), **self.__dict__
        )


class Split:
    """Represents a single split in a transaction"""

    def __init__(self, accountDb, e):
        self.accountDb = accountDb
        self.reconciled = e.find("split:reconciled-state", nss).text == "y"
        self.accountId = e.find("split:account", nss).text
        self.memo = e.find('split:memo', nss)
        accountDb[self.accountId].used = True

        # Some special treatment for value and quantity
        rawValue = e.find("split:value", nss).text
        self.value = self.convertValue(rawValue)

        # Quantity is the amount on the commodity of the account
        rawQuantity = e.find("split:quantity", nss).text
        self.quantity = self.convertValue(rawQuantity)

    def getAccount(self):
        return self.accountDb[self.accountId]

    def toLedgerFormat(self, commodity, allCleared=False, useSymbols=False, payeeMetaData=False):
        """Outputs a string for each transaction split formatted for ledger"""
        
        outPattern = "    {flag}{accountName}{spaces}{value}{memo}"

        if commodity == self.getAccount().commodity:
            if useSymbols:
                value = "{commodity}{value:,.2f}".format(commodity=commodity, value=float(self.value))
            else:
                value = "{value:,.2f} {commodity}".format(commodity=commodity,
                                                 value=float(self.value))
        else:
            if useSymbols:
                conversion = "{destCmdty}{destValue} @@ {commodity}{value}"
            else:
                conversion = "{destValue} {destCmdty} @@ {value} {commodity}"
                
            realValue = self.value[1:] if self.value.startswith('-') else self.value
            value = conversion.format(destValue=self.quantity,
                                      destCmdty=self.getAccount().commodity,
                                      value=realValue,
                                      commodity=commodity
            )
            
        # Set the value for the flag, account name, memo, and number of spaces
        # between the account name and value
        if (self.reconciled and not(allCleared)):
            flag = "* "
        else:
            flag = ""
        accountName = self.getAccount().fullName()
        numSpaces = 76 - len(flag) - len(accountName) - len(value)
        spaces = "".join([" " + " " * numSpaces])
        memo = ""
        if self.memo is not None and payeeMetaData:
            memo = "  ; Payee: " + self.memo.text
            
        return outPattern.format(
            flag=flag,
            accountName=accountName,
            spaces=spaces,
            value=value,
            memo=memo,
        )

    def convertValue(self, rawValue):
        (intValue, decPoint) = rawValue.split("/")

        n = len(decPoint) - 1
        signFlag = intValue.startswith("-")
        if signFlag:
            intValue = intValue[1:]
        if len(intValue) < n + 1:
            intValue = "0" * (n + 1 - len(intValue)) + intValue
        if signFlag:
            intValue = "-" + intValue
        return intValue[:-n] + "." + intValue[-n:]


class Transaction:
    def __init__(self, accountDb, e, useSymbols=False):
        self.accountDb = accountDb
        self.date = dateutil.parser.parse(e.find("trn:date-posted/ts:date", nss).text)
        self.useSymbols = useSymbols
        if self.useSymbols:
            self.commodity = getCurrencySymbol(e.find("trn:currency/cmdty:id", nss).text)
        else:
            self.commodity = e.find("trn:currency/cmdty:id", nss).text
        self.description = e.find("trn:description", nss).text
        self.splits = [
            Split(accountDb, s) for s in e.findall("trn:splits/trn:split", nss)
        ]
        
    def toLedgerFormat(self, allCleared=False, dateFmt="%Y-%m-%d", payeeMetaData=False):
        """Convert a Gnucash transaction to a multi-line string formatted for ledger."""
        if allCleared:
            outPattern = "{date} * {description}\n" "{splits}\n"
            splits = "\n".join(s.toLedgerFormat(self.commodity, allCleared=True, useSymbols=self.useSymbols) for s in self.splits)
        else:
            outPattern = "{date} {description}\n" "{splits}\n"
            splits = "\n".join(
                s.toLedgerFormat(self.commodity,
                                 allCleared=False,
                                 useSymbols=self.useSymbols,
                                 payeeMetaData=payeeMetaData,
                ) for s in self.splits)

        return outPattern.format(
            date=self.date.strftime(dateFmt),
            description=self.description,
            splits=splits,
        )


class emacsHeader:
    """Creates a heading to be placed at the top of the ledger file to be read by Emacs ledger-mode"""

    def __init__(self, filename=""):
        self.today = date.today()
        self.filename = filename
                    

    def __str__(self):
        header = (
        ";; -*- Mode: ledger -*- \n"
        ";; \n"
        ";; Filename: {filename} \n"
        ";; Description: Gnucash transaction journal converted with gcash2ledger.py\n"
        ";; Time-stamp: <{date}> \n\n\n"
        )

        return header.format(filename=self.filename, date=self.today)


class GnucashData:
    
    def __init__(self, inputFile, useSymbols=False, showProgress=False):

        xmlETree = xml.etree.ElementTree.parse(inputFile).getroot()
        book = xmlETree.find("gnc:book", nss)
        
        # Find all commodities
        self.commodities = []
        if showProgress:
            print("Gathering commodity descriptions:")
        for cmdty in tqdm(book.findall("gnc:commodity", nss), disable=not(showProgress)):
            self.commodities.append(Commodity(cmdty, useSymbols=useSymbols))

        # Find all accounts
        self.accountDb = {}
        if showProgress:
            print("Gathering account descriptions:")
        for acc in tqdm(book.findall("gnc:account", nss), disable=not(showProgress)):
            Account(self.accountDb, acc, useSymbols=useSymbols)
                
        # Finally, find all transactions
        self.transactions = []
        if showProgress:
            print("Gathering transactions:")
        for xact in tqdm(book.findall("gnc:transaction", nss), disable=not(showProgress)):
            self.transactions.append(Transaction(self.accountDb, xact, useSymbols=useSymbols))

            
def convert2Ledger(args):
    """Reads a Gnucash XML file and converts it to a ledger file."""
    
    allCleared = args.cleared if args.cleared else False
    useSymbols = args.use_symbols if args.use_symbols else False
    showProgress = args.show_progress if args.show_progress else False
    payeeMetaData = args.payee_metadata if args.payee_metadata else False
    
    gcashData = GnucashData(args.input, useSymbols=useSymbols, showProgress=showProgress)
    # Generate output

    # Add a header for ledger-mode in Emacs if requested
    if args.emacs_header:
        filename = ""
        if args.output is not None:
            filename = args.output[0]
        output = str(emacsHeader(filename=filename))
    else:
        output = ""

    # Add the commodities definitions unless not requested
    if not args.no_commodity_defs:
        output += ";; Commodity Definitions\n\n"
        
        if showProgress:
            print("Converting commodity descriptions to ledger format:")
            
        for c in tqdm(gcashData.commodities, disable=not(showProgress)):
            output += "\n"
            output += c.toLedgerFormat()
            
    # Output all accounts if requested
    if not args.no_account_defs:
        output += "\n\n;; Account Definitions\n\n"
        
        if showProgress:
            print("Converting account descriptions to ledger format:")
            
        for a in tqdm(gcashData.accountDb.values(), disable=not(showProgress)):
            if a.used:
                output += "\n"
                output += a.toLedgerFormat()
                
    # And finally, Output all transactions
    if not args.no_transactions:
        output += "\n\n;;Transactions\n\n"
        
        if showProgress:
            print("Converting transactions to ledger format:")        
            
        for t in tqdm(sorted(gcashData.transactions, key=lambda x: x.date), disable=not(showProgress)):
            output += "\n"
            output += t.toLedgerFormat(
                allCleared=allCleared,
                dateFmt=args.date_format[0],
                payeeMetaData=payeeMetaData,
            )
            
    return output


def getCurrencySymbol(currencyCode):
    """ Gets the currency symbol based on the three-letter currency code otherwise
    returns the currency code"""
    
    return CurrencySymbols.get_symbol(currencyCode) or currencyCode


def createParser():
    """ Creates the default ArgumentParser object for the script and returns that object. """

    parser = argparse.ArgumentParser(
        description="Converts a Gnucash XML file to a text file that can be processed by the Ledger and hledger command line programs.",
        add_help=True,
        epilog="NOTE: Gnucash files must be saved as uncompressed XML for the conversion to work!",
    )
    parser.version = "0.1"
    parser.add_argument("input", help="a Gnucash XML file to be read")
    parser.add_argument(
        "-c",
        "--cleared",
        help="Marks all transactions as cleared and place a cleared (*) mark before the transaction heading.",
        action="store_true",
    )
    parser.add_argument(
        "-d",
        "--date-format",
        help="A string representing the desired format of dates in the ledger file. Defaults to the ISO standard format: '%%Y-%%m-%%d'.",
        action="store",
        type=str,
        nargs=1,
        default=["%Y-%m-%d"],
    )
    parser.add_argument(
        "-e",
        "--emacs-header",
        help="Adds a default header for ledger-mode in Emacs.",
        action="store_true",
    )
    parser.add_argument(
        "-f",
        "--force-clobber",
        help="Force clobbering of and output file i the file already exists. If this option is provided, the output file will overwrite the existing file with the same name.",
        action="store_true",
    )
    parser.add_argument(
        "-na",
        "--no-account-defs",
        help="Prevents output of account definitions to the output file.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "-nc",
        "--no-commodity-defs",
        help="Prevent output of commodities to the output file.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "-nt",
        "--no-transactions",
        help="Prevent output of transactions to the output file. NOTE: This will cause ledger to throw an error if executed on this file.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Name of file to store the output results.",
        action="store",
        type=str,
        nargs=1,
        metavar="FILENAME",
    )
    parser.add_argument(
        "-p",
        "--show-progress",
        help="Show script status progress while reading and writing data.",
        action="store_true",
    )
    parser.add_argument(
        "--payee-metadata",
        help="Takes the information entered into the 'Description' field in Gnucash splits and adds them as a tagged '; Payee:' memo for the corresponding transaction split.",
        action="store_true",
    )
    parser.add_argument(
        "-s",
        "--use-symbols",
        help="Replaces currency codes with currency symbols.",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version"
    )
    
    return parser


if __name__ == "__main__":
    
    # Create the ArgumentParser object to collect command line arguments
    parser = createParser()
    
    # Parse the command line arguments and store as args
    args = parser.parse_args()

    # If output file is given, write data to a text file.
    if args.output:
        
        # Check if a file with the same name as output file exists
        if os.path.exists(args.output[0]) and not args.force_clobber:
            print(
                "File {outfile} exists.\nPlease specify a new name or run"
                "script again with -f to force clobbering of existing "
                "file".format(outfile=args.output[0]))
        else:
            with open(args.output[0], "w") as outFile:
                outFile.write(convert2Ledger(args))
                
    # If no output file is given print data to stdout
    else:
        print(convert2Ledger(args))