# -*- coding: utf-8 -*-
#
# LookupDomainsFilter uses OpenDNS to lookup the values in 'osxcollector_domains' and adds the 'osxcollector_opendns' key.
#
from collections import namedtuple

from threat_intel.opendns import InvestigateApi

from osxcollector.output_filters.base_filters.output_filter import run_filter_main
from osxcollector.output_filters.base_filters.threat_feed import ThreatFeedFilter
from osxcollector.output_filters.util.blacklist import create_blacklist
from osxcollector.output_filters.util.config import config_get_deep


class LookupDomainsFilter(ThreatFeedFilter):

    """Uses OpenDNS to lookup the values in 'osxcollector_domains' and adds the 'osxcollector_opendns' key."""

    # Domain categories to consider suspicious
    SUSPICIOUS_CATEGORIES = [
        'Adware',
        'Botnet',
        'Typo Squatting',
        'Drive-by Downloads/Exploits',
        'Mobile Threats',
        'High Risk Sites and Locations',
        'Malware',
        'Phishing'
    ]

    SecurityCheck = namedtuple('SecurityCheck', ['key', 'min', 'max', 'threshold'])
    SECURITY_CHECKS = [
        # Domain Generation Algorithm. This score is generated based on the likeliness of the domain name being
        # generated by an algorithm rather than a human. This algorithm is designed to identify domains which have
        # been created using an automated randomization strategy, which is a common evasion technique in malware kits
        # or botnets. This score ranges from -100 (suspicious) to 0 (benign)
        # <http://labs.opendns.com/2013/10/24/mysterious-dga-lets-investigate-sgraph/>
        SecurityCheck('dga_score', -100, 0, -70),

        # Suspicious rank for a domain that reviews based on the lookup behavior of client IP for the domain.
        # Securerank is designed to identify hostnames requested by known infected clients but never requested
        # by clean clients, assuming these domains are more likely to be bad.
        # Scores returned range from -100 (suspicious) to 100 (benign).
        # <http://labs.opendns.com/2013/03/28/secure-rank-a-large-scale-discovery-algorithm-for-predictive-detection/>
        SecurityCheck('securerank2', -100, 100, -10),

        # ASN reputation score, ranges from -100 to 0 with -100 being very suspicious
        SecurityCheck('asn_score', -100, 0, -3),

        # Prefix ranks domains given their IP prefixes (An IP prefix is the first three octets in an IP address)
        # and the reputation score of these prefixes.
        # Ranges from -100 to 0, -100 being very suspicious
        SecurityCheck('prefix_score', -100, 0, -12),

        # RIP ranks domains given their IP addresses and the reputation score of these IP addresses.
        # Ranges from -100 to 0, -100 being very suspicious
        SecurityCheck('rip_score', -100, 0, -25)
    ]

    SECURITY_BAD_KEYS = [
        # The name of any known attacks associated with this domain.
        # Returns blank is no known threat associated with domain.
        'attack',

        # The type of the known attack, such as botnet or APT.
        # Returns blank if no known threat associated with domain.
        'threat_type'
    ]

    def __init__(self, lookup_when=None, **kwargs):
        super(LookupDomainsFilter, self).__init__('osxcollector_domains', 'osxcollector_opendns',
                                                  lookup_when=lookup_when, name_of_api_key='opendns', **kwargs)
        self._whitelist = create_blacklist(config_get_deep('domain_whitelist'))

    def _lookup_iocs(self, all_iocs):
        """Caches the OpenDNS info for a set of domains.

        Domains on a whitelist will be ignored.
        First, lookup the categorization details for each domain.
        Next, if the categorization seems suspicious or unknown, lookup detailed security info.
        Finally, if the categorization or security info is suspicious, save the threat info.

        Args:
            all_iocs: an enumerable of string domain names.
        Returns:
            A dict {domain: opendns_info}
        """
        threat_info = {}

        cache_file_name = config_get_deep('opendns.LookupDomainsFilter.cache_file_name', None)
        investigate = InvestigateApi(self._api_key, cache_file_name=cache_file_name)

        iocs = filter(lambda x: not self._whitelist.match_values(x), all_iocs)

        categorized = investigate.categorization(iocs)

        # Mark the categorization as suspicious
        for domain in categorized.keys():
            categorized[domain]['suspicious'] = self._is_category_info_suspicious(categorized[domain])

        # Decide which values to lookup security info for
        iocs = filter(lambda domain: self._should_get_security_info(domain, categorized[domain]), categorized.keys())

        security = investigate.security(iocs)

        for domain in security.keys():
            security[domain]['suspicious'] = self._is_security_info_suspicious(security[domain])

        for domain in security.keys():
            if self._should_store_ioc_info(categorized[domain], security[domain]):
                threat_info[domain] = {
                    'domain': domain,
                    'categorization': categorized[domain],
                    'security': self._trim_security_result(security[domain]),
                    'link': 'https://investigate.opendns.com/domain-view/name/{0}/view'.format(domain.encode('utf-8', errors='ignore'))
                }

        return threat_info

    def _is_category_info_suspicious(self, category_info):
        """Figure out whether the categorization info is suspicious.

        Args:
            category_info: A dict of info returned by the OpenDNS categorization call
        Returns:
            boolean
        """
        status = category_info['status']
        content_categories = category_info['content_categories']
        security_categories = category_info['security_categories']

        return -1 == status or len(security_categories) or any([cat in self.SUSPICIOUS_CATEGORIES for cat in content_categories])

    def _should_get_security_info(self, domain, category_info):
        """Figure out whether the info on the domain is interesting enough to gather more data.

        If the domain isn't categorized, or is categorized as suspicious, get security info.

        Args:
            domain: A string domain
            category_info: A dict of info returned by the OpenDNS categorization call
        Returns:
            boolean
        """
        status = category_info['status']
        content_categories = category_info['content_categories']
        security_categories = category_info['security_categories']

        return self._is_category_info_suspicious(category_info) or (0 == status and 0 == len(content_categories)
                                                                    and 0 == len(security_categories))

    def _is_security_info_suspicious(self, security_info):
        """Analyzes info from OpenDNS and makes a boolean determination of suspicious or not.

        Either looks for low values for a specific set of properties, looks for known participation in
        a threat campaign, or looks for unknown domains.

        Args:
            security_info: The result of a call to the security endpoint
        Returns:
            boolean
        """
        # Categorization of site
        if any([security_info.get(key, None) for key in self.SECURITY_BAD_KEYS]):
            return True

        for security_check in self.SECURITY_CHECKS:
            if security_info.get(security_check.key, security_check.max) <= security_check.threshold:
                return True

        if not security_info.get('found', False):
            return True

        return False

    def _should_store_ioc_info(self, category_info, security_info):
        """Figure out whether the data gathered is interesting enough to store in the output.

        Args:
            category_info: A dict of info returned by the OpenDNS categorization call
            security_info: A dict of info returned by the OpenDNS security call
        Returns:
            boolean
        """
        return category_info['suspicious'] or security_info['suspicious']

    def _trim_security_result(self, security_info):
        """Converts the results of a security call into a smaller dict.

        Args:
            security_info: The result of a call to the security endpoint.
        Returns:
            A dict
        """
        # dga_score sometimes has the wrong sign, fix that please
        dga_score = security_info.get('dga_score', 0)
        if dga_score > 0:
            security_info['dga_score'] = -1 * dga_score

        # There's a lot of info in the security_info, trim it
        result = {}
        for security_check in self.SECURITY_CHECKS:
            if security_check.key in security_info:
                result[security_check.key] = security_info[security_check.key]
        for key in self.SECURITY_BAD_KEYS:
            if key in security_info:
                result[key] = security_info[key]

        result['found'] = security_info.get('found', False)

        return result


def main():
    run_filter_main(LookupDomainsFilter)


if __name__ == "__main__":
    main()
