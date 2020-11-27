"""Builder for Beancount grammar.
"""
__copyright__ = "Copyright (C) 2015-2016  Martin Blais"
__license__ = "GNU GPLv2"

import collections
import copy
import re
import sys
import traceback
from os import path
from datetime import date
from decimal import Decimal

from beancount.core.number import ZERO
from beancount.core.number import MISSING
from beancount.core.amount import Amount
from beancount.core import display_context
from beancount.core.position import CostSpec
from beancount.core.data import Transaction
from beancount.core.data import Balance
from beancount.core.data import Open
from beancount.core.data import Close
from beancount.core.data import Commodity
from beancount.core.data import Pad
from beancount.core.data import Event
from beancount.core.data import Query
from beancount.core.data import Price
from beancount.core.data import Note
from beancount.core.data import Document
from beancount.core.data import Custom
from beancount.core.data import new_metadata
from beancount.core.data import Posting
from beancount.core.data import Booking
from beancount.core.data import EMPTY_SET

from beancount.parser import lexer
from beancount.parser import options
from beancount.core import account
from beancount.core import data


ParserError = collections.namedtuple('ParserError', 'source message entry')
ParserSyntaxError = collections.namedtuple('ParserSyntaxError', 'source message entry')
DeprecatedError = collections.namedtuple('DeprecatedError', 'source message entry')



# Key-value pairs. This is used to hold meta-data attachments temporarily.
#
# Attributes:
#  key: A string, the name of the key.
#  value: Any object.
KeyValue = collections.namedtuple('KeyValue', 'key value')

# Value-type pairs. This is used to represent custom values where the concrete
# datatypes aren't matching those which are found in the parser.
#
# Attributes:
#  value: Any object.
#  dtype: The datatype of the object.
ValueType = collections.namedtuple('ValueType', 'value dtype')

# Convenience holding class for amounts with per-share and total value.
#
# Attributes:
#   number_per: A Decimal instance, the cost/price per unit.
#   number_total: A Decimal instance, the total cost/price.
#   currency: A string, the commodity of the amount.
CompoundAmount = collections.namedtuple('CompoundAmount',
                                        'number_per number_total currency')


# A unique token used to indicate a merge of the lots of an inventory.
MERGE_COST = '***'


def valid_account_regexp(options):
    """Build a regexp to validate account names from the options.

    Args:
      options: A dict of options, as per beancount.parser.options.
    Returns:
      A string, a regular expression that will match all account names.
    """
    names = map(options.__getitem__, ('name_assets',
                                      'name_liabilities',
                                      'name_equity',
                                      'name_income',
                                      'name_expenses'))

    # Replace the first term of the account regular expression with the specific
    # names allowed under the options configuration. This code is kept in sync
    # with {5672c7270e1e}.
    return re.compile("(?:{})(?:{}{})+".format('|'.join(names),
                                               account.sep,
                                               account.ACC_COMP_NAME_RE))


# A temporary data structure used during parsing to hold and accumulate the
# fields being parsed on a transaction line. Because we want to be able to parse
# these in arbitrary order, we have to accumulate the fields and then unpack
# them intelligently in the transaction callback.
#
# Attributes:
#  tags: a set object  of the tags to be applied to this transaction.
#  links: a set of link strings to be applied to this transaction.
TagsLinks = collections.namedtuple('TagsLinks', 'tags links')


class Builder(lexer.LexBuilder):
    """A builder used by the lexer and grammar parser as callbacks to create
    the data objects corresponding to rules parsed from the input file."""

    # pylint: disable=too-many-instance-attributes
    def __init__(self):
        lexer.LexBuilder.__init__(self)

        # The result from running the parser, a list of entries.
        self.entries = []

        # Accumulated and unprocessed options.
        self.options = copy.deepcopy(options.OPTIONS_DEFAULTS)

        # A mapping of all the accounts created.
        self.accounts = {}

        # Make the account regexp more restrictive than the default: check
        # types. Warning: This overrides the value in the base class.
        self.account_regexp = valid_account_regexp(self.options)

        # A display context builder.
        self.dcontext = display_context.DisplayContext()
        self.display_context_update = self.dcontext.update

    def _dcupdate(self, number, currency):
        """Update the display context."""
        if isinstance(number, Decimal) and currency and currency is not MISSING:
            self.display_context_update(number, currency)

    def finalize(self):
        """Finalize the parser, check for final errors and return the triple.

        Returns:
          A triple of
            entries: A list of parsed directives, which may need completion.
            errors: A list of errors, hopefully empty.
            options_map: A dict of options.
        """
        # Weave the commas option in the DisplayContext itself, so it propagates
        # everywhere it is used automatically.
        self.dcontext.set_commas(self.options['render_commas'])

        return (self.get_entries(), self.errors, self.get_options())

    def get_entries(self):
        """Return the accumulated entries.

        Returns:
          A list of sorted directives.
        """
        return sorted(self.entries, key=data.entry_sortkey)

    def get_options(self):
        """Return the final options map.

        Returns:
          A dict of option names to options.
        """
        # Build and store the inferred DisplayContext instance.
        self.options['dcontext'] = self.dcontext

        return self.options

    def get_long_string_maxlines(self):
        """See base class."""
        return self.options['long_string_maxlines']

    def store_result(self, filename, lineno, entries):
        """Start rule stores the final result here.

        Args:
          entries: A list of entries to store.
        """
        if entries:
            self.entries = entries
        # Also record the name of the processed file.
        self.options['filename'] = filename

    def build_grammar_error(self, filename, lineno, exc_value,
                            exc_type=None, exc_traceback=None):
        """Build a grammar error and appends it to the list of pending errors.

        Args:
          filename: The current filename
          lineno: The current line number
          excvalue: The exception value, or a str, the message of the error.
          exc_type: An exception type, if an exception occurred.
          exc_traceback: A traceback object.
        """
        if exc_type is not None:
            assert not isinstance(exc_value, str)
            strings = traceback.format_exception_only(exc_type, exc_value)
            tblist = traceback.extract_tb(exc_traceback)
            filename, lineno, _, __ = tblist[0]
            message = '{} ({}:{})'.format(strings[0], filename, lineno)
        else:
            message = str(exc_value)
        meta = new_metadata(filename, lineno)
        self.errors.append(
            ParserSyntaxError(meta, message, None))

    def option(self, filename, lineno, key, value):
        """Process an option directive.

        Args:
          filename: current filename.
          lineno: current line number.
          key: option's key (str)
          value: option's value
        """
        if key not in self.options:
            meta = new_metadata(filename, lineno)
            self.errors.append(
                ParserError(meta, "Invalid option: '{}'".format(key), None))

        elif key in options.READ_ONLY_OPTIONS:
            meta = new_metadata(filename, lineno)
            self.errors.append(
                ParserError(meta, "Option '{}' may not be set".format(key), None))

        else:
            option_descriptor = options.OPTIONS[key]

            # Issue a warning if the option is deprecated.
            if option_descriptor.deprecated:
                assert isinstance(option_descriptor.deprecated, str), "Internal error."
                meta = new_metadata(filename, lineno)
                self.errors.append(
                    DeprecatedError(meta, option_descriptor.deprecated, None))

            # Rename the option if it has an alias.
            if option_descriptor.alias:
                key = option_descriptor.alias
                option_descriptor = options.OPTIONS[key]

            # Convert the value, if necessary.
            if option_descriptor.converter:
                try:
                    value = option_descriptor.converter(value)
                except ValueError as exc:
                    meta = new_metadata(filename, lineno)
                    self.errors.append(
                        ParserError(meta,
                                    "Error for option '{}': {}".format(key, exc),
                                    None))
                    return

            option = self.options[key]
            if isinstance(option, list):
                # Append to a list of values.
                option.append(value)

            elif isinstance(option, dict):
                # Set to a dict of values.
                if not (isinstance(value, tuple) and len(value) == 2):
                    self.errors.append(
                        ParserError(
                            meta, "Error for option '{}': {}".format(key, value), None))
                    return
                dict_key, dict_value = value
                option[dict_key] = dict_value

            elif isinstance(option, bool):
                # Convert to a boolean.
                if not isinstance(value, bool):
                    value = (value.lower() in {'true', 'on'}) or (value == '1')
                self.options[key] = value

            else:
                # Set the value.
                self.options[key] = value

            # Refresh the list of valid account regexps as we go along.
            if key.startswith('name_'):
                # Update the set of valid account types.
                self.account_regexp = valid_account_regexp(self.options)
            elif key == 'insert_pythonpath':
                # Insert the PYTHONPATH to this file when and only if you
                # encounter this option.
                sys.path.insert(0, path.dirname(filename))

    def include(self, filename, lineno, include_filename):
        """Process an include directive.

        Args:
          filename: current filename.
          lineno: current line number.
          include_name: A string, the name of the file to include.
        """
        self.options['include'].append(include_filename)

    def plugin(self, filename, lineno, plugin_name, plugin_config):
        """Process a plugin directive.

        Args:
          filename: current filename.
          lineno: current line number.
          plugin_name: A string, the name of the plugin module to import.
          plugin_config: A string or None, an optional configuration string to
            pass in to the plugin module.
        """
        self.options['plugin'].append((plugin_name, plugin_config))

    def handle_list(self, filename, lineno, object_list, new_object):
        """Handle a recursive list grammar rule, generically.

        Args:
          object_list: the current list of objects.
          new_object: the new object to be added.
        Returns:
          The new, updated list of objects.
        """
        if object_list is None:
            object_list = []
        if new_object is not None:
            object_list.append(new_object)
        return object_list

    def open(self, filename, lineno, date, account, currencies, booking_str, kvlist):
        """Process an open directive.

        Args:
          filename: The current filename.
          lineno: The current line number.
          date: A datetime object.
          account: A string, the name of the account.
          currencies: A list of constraint currencies.
          booking_str: A string, the booking method, or None if none was specified.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Open object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        error = False
        if booking_str:
            try:
                # Note: Somehow the 'in' membership operator is not defined on Enum.
                booking = Booking[booking_str]
            except KeyError:
                # If the per-account method is invalid, set it to the global
                # default method and continue.
                booking = self.options['booking_method']
                error = True
        else:
            booking = None

        entry = Open(meta, date, account, currencies, booking)
        if error:
            self.errors.append(ParserError(meta,
                                           "Invalid booking method: {}".format(booking_str),
                                           entry))
        return entry

    def close(self, filename, lineno, date, account, kvlist):
        """Process a close directive.

        Args:
          filename: The current filename.
          lineno: The current line number.
          date: A datetime object.
          account: A string, the name of the account.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Close object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Close(meta, date, account)

    def commodity(self, filename, lineno, date, currency, kvlist):
        """Process a close directive.

        Args:
          filename: The current filename.
          lineno: The current line number.
          date: A datetime object.
          currency: A string, the commodity being declared.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Close object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Commodity(meta, date, currency)

    def pad(self, filename, lineno, date, account, source_account, kvlist):
        """Process a pad directive.

        Args:
          filename: The current filename.
          lineno: The current line number.
          date: A datetime object.
          account: A string, the account to be padded.
          source_account: A string, the account to pad from.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Pad object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Pad(meta, date, account, source_account)

    def event(self, filename, lineno, date, event_type, description, kvlist):
        """Process an event directive.

        Args:
          filename: the current filename.
          lineno: the current line number.
          date: a datetime object.
          event_type: a str, the name of the event type.
          description: a str, the event value, the contents.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Event object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Event(meta, date, event_type, description)

    def query(self, filename, lineno, date, query_name, query_string, kvlist):
        """Process a document directive.

        Args:
          filename: the current filename.
          lineno: the current line number.
          date: a datetime object.
          query_name: a str, the name of the query.
          query_string: a str, the SQL query itself.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Query object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Query(meta, date, query_name, query_string)

    def price(self, filename, lineno, date, currency, amount, kvlist):
        """Process a price directive.

        Args:
          filename: the current filename.
          lineno: the current line number.
          date: a datetime object.
          currency: the currency to be priced.
          amount: an instance of Amount, that is the price of the currency.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Price object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Price(meta, date, currency, amount)

    def note(self, filename, lineno, date, account, comment, kvlist):
        """Process a note directive.

        Args:
          filename: The current filename.
          lineno: The current line number.
          date: A datetime object.
          account: A string, the account to attach the note to.
          comment: A str, the note's comments contents.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Note object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Note(meta, date, account, comment)

    def document(self, filename, lineno, date, account, document_filename, tags,links,
                 kvlist):
        """Process a document directive.

        Args:
          filename: the current filename.
          lineno: the current line number.
          date: a datetime object.
          account: an Account instance.
          document_filename: a str, the name of the document file.
          tags: A set of tag strings.
          links: A set of link strings.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Document object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        if not path.isabs(document_filename):
            document_filename = path.abspath(path.join(path.dirname(filename),
                                                       document_filename))
        tags, links = self._finalize_tags_links(tags_links.tags, tags_links.links)
        return Document(meta, date, account, document_filename, tags, links)

    def custom(self, filename, lineno, date, dir_type, custom_values, kvlist):
        """Process a custom directive.

        Args:
          filename: the current filename.
          lineno: the current line number.
          date: a datetime object.
          dir_type: A string, a type for the custom directive being parsed.
          custom_values: A list of the various tokens seen on the same line.
          kvlist: a list of KeyValue instances.
        Returns:
          A new Custom object.
        """
        meta = new_metadata(filename, lineno, kvlist)
        return Custom(meta, date, dir_type, custom_values)

    def custom_value(self, filename, lineno, value, dtype=None):
        """Create a custom value object, along with its type.

        Args:
          value: One of the accepted custom values.
        Returns:
          A pair of (value, dtype) where 'dtype' is the datatype is that of the
          value.
        """
        if dtype is None:
            dtype = type(value)
        return ValueType(value, dtype)

    def _unpack_txn_strings(self, txn_strings, meta):
        """Unpack a tags_links accumulator to its payee and narration fields.

        Args:
          txn_strings: A list of strings.
          meta: A metadata dict for errors generated in this routine.
        Returns:
          A pair of (payee, narration) strings or None objects, or None, if
          there was an error.
        """
        num_strings = 0 if txn_strings is None else len(txn_strings)
        if num_strings == 1:
            payee, narration = None, txn_strings[0]
        elif num_strings == 2:
            payee, narration = txn_strings
        elif num_strings == 0:
            payee, narration = None, ""
        else:
            self.errors.append(
                ParserError(meta,
                            "Too many strings on transaction description: {}".format(
                                txn_strings), None))
            return None
        return payee, narration

    def _finalize_tags_links(self, tags, links):
        """Finally amend tags and links and return final objects to be inserted.

        Args:
          tags: A set of tag strings (warning: this gets mutated in-place).
          links: A set of link strings.
        Returns:
          A sanitized pair of (tags, links).
        """
        return (frozenset(tags) if tags else EMPTY_SET,
                frozenset(links) if links else EMPTY_SET)

    def transaction(self, filename, lineno, date, flag, txn_strings, tags, links,
                    posting_or_kv_list, active_meta):
        """Process a transaction directive.

        All the postings of the transaction are available at this point, and so the
        the transaction is balanced here, incomplete postings are completed with the
        appropriate position, and errors are being accumulated on the builder to be
        reported later on.

        This is the main routine that takes up most of the parsing time; be very
        careful with modifications here, they have an impact on performance.

        Args:
          filename: the current filename.
          lineno: the current line number.
          date: a datetime object.
          flag: a str, one-character, the flag associated with this transaction.
          txn_strings: A list of strings, possibly empty, possibly longer.
          tags: A set of tag strings.
          links: A set of link strings.
          posting_or_kv_list: a list of Posting, KeyValue or TagsLinks
            instances, to be inserted in this transaction, or None, if no
            postings have been declared.
          active_meta:
        Returns:
          A new Transaction object.
        """
        meta = new_metadata(filename, lineno)

        # Separate postings and key-values.
        explicit_meta = {}
        postings = []
        if posting_or_kv_list:
            last_posting = None
            for posting_or_kv in posting_or_kv_list:
                if isinstance(posting_or_kv, Posting):
                    postings.append(posting_or_kv)
                    last_posting = posting_or_kv
                elif isinstance(posting_or_kv, TagsLinks):
                    if postings:
                        self.errors.append(ParserError(
                            meta,
                            "Tags or links not allowed after first " +
                            "Posting: {}".format(posting_or_kv), None))
                    else:
                        tags.update(posting_or_kv.tags)
                        links.update(posting_or_kv.links)
                else:
                    if last_posting is None:
                        value = explicit_meta.setdefault(posting_or_kv.key,
                                                         posting_or_kv.value)
                        if value is not posting_or_kv.value:
                            self.errors.append(ParserError(
                                meta, "Duplicate metadata field on entry: {}".format(
                                    posting_or_kv), None))
                    else:
                        if last_posting.meta is None:
                            last_posting = last_posting._replace(meta={})
                            postings.pop(-1)
                            postings.append(last_posting)

                        value = last_posting.meta.setdefault(posting_or_kv.key,
                                                             posting_or_kv.value)
                        if value is not posting_or_kv.value:
                            self.errors.append(ParserError(
                                meta, "Duplicate posting metadata field: {}".format(
                                    posting_or_kv), None))

        # Freeze the tags & links or set to default empty values.
        tags, links = self._finalize_tags_links(tags, links)

        # Initialize the metadata fields from the set of active values.
        if active_meta:
            meta.update(active_meta)

        # Add on explicitly defined values.
        if explicit_meta:
            meta.update(explicit_meta)

        # Unpack the transaction fields.
        payee_narration = self._unpack_txn_strings(txn_strings, meta)
        if payee_narration is None:
            return None
        payee, narration = payee_narration

        # We now allow a single posting when its balance is zero, so we
        # commented out the check below. If a transaction has a single posting
        # with a non-zero balance, it'll get caught below in the booking code.
        #
        # # Detect when a transaction does not have at least two legs.
        # if postings is None or len(postings) < 2:
        #     self.errors.append(
        #         ParserError(meta,
        #                     "Transaction with only one posting: {}".format(postings),
        #                     None))
        #     return None

        # If there are no postings, make sure we insert a list object.
        if postings is None:
            postings = []

        # Create the transaction.
        return Transaction(meta, date, chr(flag),
                           payee, narration, tags, links, postings)

    # TODO(blais): Remove.
    def create_amount(self, _, __, number, currency):
        return Amount(number, currency)