from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import transaction
from content.models import EventV4  # Adjust import path as needed
import pytz
from datetime import datetime, date
from colorama import Fore, Style, init

# Initialize colorama for colored output
init(autoreset=True)


class Command(BaseCommand):
    help = 'Updates event start_dates to match their next occurrence start_datetime when different'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be changed without making any updates',
        )
        parser.add_argument(
            '--auto-confirm',
            action='store_true',
            help='Skip confirmation prompt and apply changes automatically',
        )
        parser.add_argument(
            '--event-id',
            type=int,
            help='Update only a specific event by ID',
        )
        parser.add_argument(
            '--published-only',
            action='store_true',
            help='Only process published events',
        )
        parser.add_argument(
            '--future-only',
            action='store_true',
            help='Only process events marked as not past',
        )

    def handle(self, *args, **options):
        self.stdout.write(f"{Fore.CYAN}=== Event Start Date Update Tool ==={Style.RESET_ALL}")

        # Build queryset based on options
        queryset = EventV4.objects.all()
        
        if options['event_id']:
            queryset = queryset.filter(id=options['event_id'])
            if not queryset.exists():
                raise CommandError(f"Event with ID {options['event_id']} not found")
        
        if options['published_only']:
            queryset = queryset.filter(published=True)
            
        if options['future_only']:
            queryset = queryset.filter(is_past=False)

        # Prefetch related occurrences for efficiency
        queryset = queryset.prefetch_related('occurrences')

        self.stdout.write(f"Analyzing {queryset.count()} events...")


        events_to_update = []
        
        # Analyze each event
        for event in queryset:
            update_info = self.analyze_event(event)
            if update_info:
                events_to_update.append(update_info)

        if not events_to_update:
            self.stdout.write(f"{Fore.GREEN}✓ No events need start_date updates{Style.RESET_ALL}")
            return

        # Display summary
        self.stdout.write(f"{Fore.YELLOW}Found {len(events_to_update)} events that need updates:{Style.RESET_ALL}")

        # Display details of each event to be updated
        for i, update_info in enumerate(events_to_update, 1):
            self.display_event_details(i, update_info)

        # Summary table
        self.stdout.write(f"\n{Fore.CYAN}SUMMARY:{Style.RESET_ALL}")
        self.stdout.write(f"Events to update: {len(events_to_update)}")
        
        if options['dry_run']:
            self.stdout.write(f"{Fore.BLUE}DRY RUN MODE - No changes will be made{Style.RESET_ALL}")
            return

        # Confirmation
        if not options['auto_confirm']:
            response = input(f"\n{Fore.YELLOW}Do you want to proceed with these updates? (y/N): {Style.RESET_ALL}")
            if response.lower() not in ['y', 'yes']:
                self.stdout.write(f"{Fore.RED}Operation cancelled{Style.RESET_ALL}")
                return

        # Apply updates
        self.apply_updates(events_to_update)

    def analyze_event(self, event):
        """Analyze an event to see if its start_date needs updating"""
        now = timezone.now()
        
        # Get the next future occurrence
        next_occurrence = event.occurrences.filter(
            start_datetime__gt=now
        ).order_by('start_datetime').first()
        
        if not next_occurrence:
            return None  # No future occurrences
        
        next_start = next_occurrence.start_datetime
        
        # Make timezone aware if needed
        if timezone.is_naive(next_start):
            next_start = timezone.make_aware(next_start, timezone=pytz.UTC)
        
        current_start = event.start_date
        
        # Handle different field types (date vs datetime)
        needs_update = False
        new_value = None
        
        if isinstance(current_start, date) and not isinstance(current_start, datetime):
            # start_date is a DateField
            if next_start.date() != current_start:
                needs_update = True
                new_value = next_start.date()
        else:
            # start_date is a DateTimeField
            if timezone.is_naive(current_start):
                current_start = timezone.make_aware(current_start, timezone=pytz.UTC)
            
            if next_start != current_start:
                needs_update = True
                new_value = next_start
        
        if needs_update:
            return {
                'event': event,
                'current_start': current_start,
                'new_start': new_value,
                'next_occurrence': next_occurrence,
                'field_type': 'date' if isinstance(new_value, date) and not isinstance(new_value, datetime) else 'datetime'
            }
        
        return None

    def display_event_details(self, index, update_info):
        """Display detailed information about an event that will be updated"""
        event = update_info['event']
        
        self.stdout.write(f"{Fore.WHITE}[{index}] Event ID: {event.id}{Style.RESET_ALL}")
        self.stdout.write(f"    Title: {event.title[:80]}{'...' if len(event.title) > 80 else ''}")
        self.stdout.write(f"    Source: {event.source}")
        self.stdout.write(f"    Published: {event.published}")
        self.stdout.write(f"    Location: {event.town or 'N/A'}")
        
        # Current vs new start date
        current = update_info['current_start']
        new = update_info['new_start']
        
        self.stdout.write(f"    {Fore.RED}Current start_date: {current}{Style.RESET_ALL}")
        self.stdout.write(f"    {Fore.GREEN}New start_date:     {new}{Style.RESET_ALL}")
        
        # Next occurrence details
        next_occ = update_info['next_occurrence']
        self.stdout.write(f"    Next occurrence: {next_occ.start_datetime} - {next_occ.end_datetime}")
        
        # Show all future occurrences for context
        now = timezone.now()
        future_occurrences = event.occurrences.filter(
            start_datetime__gt=now
        ).order_by('start_datetime')[:3]  # Show first 3
        
        if future_occurrences.count() > 1:
            self.stdout.write(f"    Future occurrences ({future_occurrences.count()} total):")
            for occ in future_occurrences:
                marker = "→" if occ == next_occ else " "
                self.stdout.write(f"      {marker} {occ.start_datetime}")
        

    def apply_updates(self, events_to_update):
        """Apply the updates to the database"""
        self.stdout.write(f"\n{Fore.CYAN}Applying updates...{Style.RESET_ALL}")
        
        updated_count = 0
        failed_count = 0
        
        with transaction.atomic():
            for update_info in events_to_update:
                try:
                    event = update_info['event']
                    new_start = update_info['new_start']
                    
                    event.start_date = new_start
                    event.save(update_fields=['start_date'])
                    
                    updated_count += 1
                    self.stdout.write(f"  {Fore.GREEN}✓{Style.RESET_ALL} Updated event {event.id}: {event.title[:50]}...")
                    
                except Exception as e:
                    failed_count += 1
                    self.stdout.write(f"  {Fore.RED}✗{Style.RESET_ALL} Failed to update event {event.id}: {str(e)}")
        
        # Final summary
        self.stdout.write(f"{Fore.CYAN}Update Complete:{Style.RESET_ALL}")
        self.stdout.write(f"  Successfully updated: {Fore.GREEN}{updated_count}{Style.RESET_ALL}")
        if failed_count > 0:
            self.stdout.write(f"  Failed updates: {Fore.RED}{failed_count}{Style.RESET_ALL}")
        
        self.stdout.write(f"\n{Fore.GREEN}All updates completed successfully!{Style.RESET_ALL}")


# Additional utility command for one-time analysis
class AnalyzeCommand(BaseCommand):
    """Separate command for analysis only"""
    help = 'Analyze events without making changes'
    
    def handle(self, *args, **options):
        from django.core.management import call_command
        call_command('update_event_start_dates', '--dry-run')