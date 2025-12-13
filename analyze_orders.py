import sqlite3
import re
from collections import defaultdict
from datetime import datetime
from rich.console import Console
from rich.table import Table

console = Console()

def extract_order_info(subject, body, from_addr, to_addr, date):
    """Extract order information from email content."""
    order_info = {
        'order_numbers': [],
        'status_keywords': [],
        'persons': set(),
        'issues': [],
        'date': date
    }
    
    # Common order number patterns
    order_patterns = [
        r'order\s*#?\s*(\d{4,})',
        r'order\s*number[:\s]*(\d{4,})',
        r'#(\d{5,})',
        r'po[:\s#]*(\d{4,})',
        r'invoice[:\s#]*(\d{4,})',
        r'confirmation[:\s#]*(\d{4,})',
    ]
    
    text = f"{subject} {body}".lower()
    
    for pattern in order_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        order_info['order_numbers'].extend(matches)
    
    # Status keywords
    status_keywords = {
        'pending': ['pending', 'waiting', 'on hold', 'processing'],
        'shipped': ['shipped', 'dispatched', 'sent', 'delivered', 'tracking'],
        'cancelled': ['cancelled', 'canceled', 'refund', 'refunded'],
        'problem': ['problem', 'issue', 'error', 'failed', 'delay', 'delayed', 'wrong', 'missing', 'damaged', 'complaint', 'urgent', 'asap'],
        'confirmed': ['confirmed', 'confirmation', 'approved', 'accepted'],
        'payment': ['payment', 'paid', 'invoice', 'receipt'],
    }
    
    for status, keywords in status_keywords.items():
        for kw in keywords:
            if kw in text:
                order_info['status_keywords'].append(status)
                break
    
    # Extract email addresses as persons involved
    email_pattern = r'[\w\.-]+@[\w\.-]+\.\w+'
    emails = re.findall(email_pattern, f"{from_addr} {to_addr} {body}")
    order_info['persons'].update(emails)
    
    # Look for issues that need rectification
    issue_patterns = [
        r'(urgent[^.]*\.)',
        r'(asap[^.]*\.)',
        r'(problem[^.]*\.)',
        r'(issue[^.]*\.)',
        r'(please\s+fix[^.]*\.)',
        r'(need\s+to[^.]*\.)',
        r'(missing[^.]*\.)',
        r'(wrong[^.]*\.)',
        r'(delayed[^.]*\.)',
        r'(complaint[^.]*\.)',
    ]
    
    for pattern in issue_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        order_info['issues'].extend(matches[:2])  # Limit to 2 matches per pattern
    
    return order_info

def analyze_emails():
    """Analyze all emails for order information."""
    db_path = 'email_archive.db'
    
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get total count
        cursor.execute('SELECT COUNT(*) as count FROM emails')
        total = cursor.fetchone()['count']
        console.print(f"\n[bold green]Total emails in database: {total}[/]")
        
        # Get all emails
        cursor.execute('''
            SELECT id, subject, from_address, to_address, date_received, body_text 
            FROM emails 
            ORDER BY date_received DESC
        ''')
        
        emails = cursor.fetchall()
        
        # Analyze
        orders = defaultdict(lambda: {
            'subjects': [],
            'statuses': set(),
            'persons': set(),
            'issues': [],
            'dates': [],
            'email_ids': []
        })
        
        all_persons = set()
        all_issues = []
        problem_emails = []
        
        for email in emails:
            body = email['body_text'] or ''
            subject = email['subject'] or ''
            
            info = extract_order_info(
                subject, 
                body, 
                email['from_address'], 
                email['to_address'],
                email['date_received']
            )
            
            # Track problems
            if 'problem' in info['status_keywords'] or info['issues']:
                problem_emails.append({
                    'id': email['id'],
                    'subject': subject,
                    'from': email['from_address'],
                    'date': email['date_received'],
                    'issues': info['issues']
                })
            
            # Track by order number
            for order_num in info['order_numbers']:
                orders[order_num]['subjects'].append(subject)
                orders[order_num]['statuses'].update(info['status_keywords'])
                orders[order_num]['persons'].update(info['persons'])
                orders[order_num]['issues'].extend(info['issues'])
                orders[order_num]['dates'].append(info['date'])
                orders[order_num]['email_ids'].append(email['id'])
            
            all_persons.update(info['persons'])
            all_issues.extend(info['issues'])
        
        # Display Orders Table
        console.print("\n[bold magenta]═══ ORDERS FOUND ═══[/]")
        
        if orders:
            order_table = Table(show_header=True, header_style="bold cyan")
            order_table.add_column("Order #", width=15)
            order_table.add_column("Status", width=20)
            order_table.add_column("Last Activity", width=20)
            order_table.add_column("Email Count", width=10)
            order_table.add_column("Persons Involved", width=40)
            
            for order_num, data in sorted(orders.items(), key=lambda x: x[1]['dates'][-1] if x[1]['dates'] else '', reverse=True)[:50]:
                statuses = ', '.join(data['statuses']) if data['statuses'] else 'Unknown'
                last_date = max(data['dates']) if data['dates'] else 'N/A'
                persons = ', '.join(list(data['persons'])[:3])
                if len(data['persons']) > 3:
                    persons += f" (+{len(data['persons'])-3} more)"
                
                order_table.add_row(
                    order_num,
                    statuses,
                    str(last_date)[:19] if last_date else 'N/A',
                    str(len(data['email_ids'])),
                    persons
                )
            
            console.print(order_table)
        else:
            console.print("[yellow]No explicit order numbers found. Analyzing by subject...[/]")
        
        # Display Issues/Problems that need rectification
        console.print("\n[bold red]═══ ISSUES REQUIRING ATTENTION ═══[/]")
        
        if problem_emails:
            issue_table = Table(show_header=True, header_style="bold red")
            issue_table.add_column("ID", width=5)
            issue_table.add_column("Date", width=20)
            issue_table.add_column("From", width=30)
            issue_table.add_column("Subject", width=50)
            
            for prob in problem_emails[:30]:  # Show top 30
                issue_table.add_row(
                    str(prob['id']),
                    str(prob['date'])[:19] if prob['date'] else 'N/A',
                    prob['from'][:30],
                    prob['subject'][:50]
                )
            
            console.print(issue_table)
            console.print(f"\n[bold]Total emails with potential issues: {len(problem_emails)}[/]")
        else:
            console.print("[green]No obvious issues detected in email subjects/bodies.[/]")
        
        # Display All Persons
        console.print("\n[bold blue]═══ ALL PERSONS INVOLVED ═══[/]")
        
        person_table = Table(show_header=True, header_style="bold blue")
        person_table.add_column("Email Address", width=50)
        
        for person in sorted(all_persons)[:50]:
            person_table.add_row(person)
        
        console.print(person_table)
        console.print(f"\n[bold]Total unique email addresses: {len(all_persons)}[/]")
        
        # Save detailed report to file
        with open('order_analysis_report.txt', 'w') as f:
            f.write("EMAIL ANALYSIS REPORT\n")
            f.write("=" * 80 + "\n")
            f.write(f"Generated: {datetime.now()}\n")
            f.write(f"Total Emails Analyzed: {total}\n\n")
            
            f.write("ORDERS FOUND\n")
            f.write("-" * 80 + "\n")
            for order_num, data in sorted(orders.items(), key=lambda x: x[1]['dates'][-1] if x[1]['dates'] else '', reverse=True):
                f.write(f"\nOrder #{order_num}\n")
                f.write(f"  Statuses: {', '.join(data['statuses']) if data['statuses'] else 'Unknown'}\n")
                f.write(f"  Email Count: {len(data['email_ids'])}\n")
                f.write(f"  Persons: {', '.join(data['persons'])}\n")
                f.write(f"  Subjects:\n")
                for subj in data['subjects'][:5]:
                    f.write(f"    - {subj}\n")
                if data['issues']:
                    f.write(f"  Issues:\n")
                    for issue in data['issues'][:3]:
                        f.write(f"    ! {issue}\n")
            
            f.write("\n\nISSUES REQUIRING IMMEDIATE ATTENTION\n")
            f.write("-" * 80 + "\n")
            for prob in problem_emails:
                f.write(f"\nEmail ID: {prob['id']}\n")
                f.write(f"  Date: {prob['date']}\n")
                f.write(f"  From: {prob['from']}\n")
                f.write(f"  Subject: {prob['subject']}\n")
                if prob['issues']:
                    for issue in prob['issues']:
                        f.write(f"  Issue: {issue}\n")
            
            f.write("\n\nALL PERSONS INVOLVED\n")
            f.write("-" * 80 + "\n")
            for person in sorted(all_persons):
                f.write(f"  {person}\n")
        
        console.print("\n[green]Full report saved to: order_analysis_report.txt[/]")

if __name__ == "__main__":
    analyze_emails()
